from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from typing import Callable

from fastapi import FastAPI, Path, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.adapters.qwen import QwenProvider, QwenProviderError, build_qwen_provider
from app.adapters.sandbox import SandboxRunner, build_sandbox_runner
from app.application.analyze import execute_analysis, failed, prepare_analysis
from app.application.errors import AnalysisFailure
from app.application.lifecycle import AnalysisJobs
from app.application.sessions import SessionCapacityExceeded, SessionStore, load_session_ttl_seconds
from app.application.workbook import execute_plan, execute_workbook_analysis, prepare_plan, prepare_workbook_analysis
from app.domain.contracts import (
    ID_PATTERN,
    SESSION_ID_PATTERN,
    AnalysisError,
    AnalysisRequest,
    AnalysisStatus,
    FailedAnalysisResult,
    SessionCreated,
    SessionCreateRequest,
    WorkbookAnalysisRequest,
    WorkbookPlanRequest,
)

logger = logging.getLogger(__name__)

# Failures caused by session or job state get non-2xx codes; analysis outcomes stay 200 with stage "failed".
ERROR_STATUS = {"SESSION_NOT_FOUND": 404, "SESSION_SCOPE_MISMATCH": 409, "PLAN_NOT_FOUND": 409, "ARTIFACTS_MISMATCH": 409, "ANALYSIS_NOT_FOUND": 404}

SessionIdPath = Path(pattern=SESSION_ID_PATTERN)
AnalysisIdPath = Path(pattern=ID_PATTERN)


def _status(status: AnalysisStatus, http_status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=http_status, content=status.model_dump(mode="json"))


def _failed_status(result: FailedAnalysisResult) -> JSONResponse:
    """A request rejected before any work started: no job exists, nothing reached Qwen or the sandbox."""
    status = AnalysisStatus(analysisId=result.analysisId, stage="failed", elapsedMs=result.elapsedMs, result=result)
    return _status(status, ERROR_STATUS.get(result.error.code, 200))


def _rejected(analysis_id: str, error: AnalysisFailure | QwenProviderError) -> JSONResponse:
    return _failed_status(failed(analysis_id, time.perf_counter(), error))


def create_app(
    sessions: SessionStore | None = None,
    provider_factory: Callable[[], QwenProvider] = build_qwen_provider,
    sandbox: SandboxRunner | None = None,
    jobs: AnalysisJobs | None = None,
) -> FastAPI:
    # Sessions, analysis jobs and sandbox slots live in this process only (AD-6). Run one API process,
    # or route every request of a pane to the same one; see docs/deployment/production-checklist.md.
    store = sessions if sessions is not None else SessionStore(ttl_seconds=load_session_ttl_seconds())
    # The only port that executes generated code; without a container runtime it fails, never runs on the host.
    runner = sandbox if sandbox is not None else build_sandbox_runner()
    registry = jobs if jobs is not None else AnalysisJobs.from_env()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async def sweep() -> None:
            while True:
                await asyncio.sleep(min(60, store.ttl_seconds / 2))
                store.purge_expired()

        task = asyncio.create_task(sweep())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="FinLens API", lifespan=lifespan)
    app.state.sessions = store
    app.state.jobs = registry

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_, __):
        return JSONResponse(status_code=422, content={"analysisId": "unknown", "status": "failed", "elapsedMs": 0, "error": {"code": "REQUEST_INVALID", "message": "Запрос анализа имеет недопустимый формат или превышает лимиты.", "nextAction": "Выберите меньший диапазон и повторите вопрос."}})

    @app.post("/api/sessions", status_code=201)
    def create_session(request: SessionCreateRequest):
        try:
            session_id = store.create(request.scope)
        except SessionCapacityExceeded:
            logger.warning("session_rejected code=SESSION_CAPACITY")
            return JSONResponse(status_code=503, content={"status": "failed", "error": {"code": "SESSION_CAPACITY", "message": "FinLens сейчас обслуживает слишком много сеансов.", "nextAction": "Повторите попытку через несколько минут."}})
        return SessionCreated(sessionId=session_id, scope=request.scope, ttlSeconds=store.ttl_seconds)

    def end_session(session_id: str) -> Response:
        # Unfinished analyses of the session are cancelled too: their jobs, containers and plans are dropped.
        registry.cancel_session(session_id)
        store.close(session_id)
        return Response(status_code=204)

    @app.delete("/api/sessions/{session_id}", status_code=204)
    def delete_session(session_id: str = SessionIdPath):
        return end_session(session_id)

    # POST alias so the task pane can clear its session with navigator.sendBeacon on unload.
    @app.post("/api/sessions/{session_id}/close", status_code=204)
    def close_session(session_id: str = SessionIdPath):
        return end_session(session_id)

    def finished_or_none(analysis_id: str) -> JSONResponse | None:
        # A cancel may arrive before the start request: a cancelled or finished ID never starts again.
        current = registry.status(analysis_id)
        return _status(current) if current is not None and current.result is not None else None

    @app.post("/api/analysis")
    def create_analysis(request: AnalysisRequest):
        if (existing := finished_or_none(request.analysisId)) is not None:
            return existing
        try:
            provider = provider_factory()
            prepared = prepare_analysis(request, store)
        except (AnalysisFailure, QwenProviderError) as error:
            return _rejected(request.analysisId, error)
        status, code = registry.start(
            request.analysisId,
            lambda control: execute_analysis(prepared, provider, store, runner, control),
            stage="preparing_answer",
            session_id=request.sessionId,
        )
        return _status(status, code)

    @app.post("/api/sessions/{session_id}/workbook/plan")
    def create_workbook_plan(request: WorkbookPlanRequest, session_id: str = SessionIdPath):
        if (existing := finished_or_none(request.analysisId)) is not None:
            return existing
        try:
            provider = provider_factory()
            prepared = prepare_plan(session_id, request, store)
        except (AnalysisFailure, QwenProviderError) as error:
            return _rejected(request.analysisId, error)
        status, code = registry.start(
            request.analysisId,
            lambda control: execute_plan(prepared, provider, store, control),
            stage="selecting_artifacts",
            session_id=session_id,
            # Cancelling a planned analysis also drops its single-use plan from the session.
            on_cancel=lambda: store.discard_pending_plan(session_id, request.analysisId),
        )
        return _status(status, code)

    @app.post("/api/sessions/{session_id}/workbook/analysis")
    def create_workbook_analysis(request: WorkbookAnalysisRequest, session_id: str = SessionIdPath):
        if (existing := finished_or_none(request.analysisId)) is not None:
            return existing
        try:
            provider = provider_factory()
            prepared = prepare_workbook_analysis(session_id, request, store)
        except (AnalysisFailure, QwenProviderError) as error:
            rejected = failed(request.analysisId, time.perf_counter(), error)
            # The planned analysis ends here: its single-use plan is gone, so it must not linger as "planned".
            registry.abandon_planned(request.analysisId, rejected)
            return _failed_status(rejected)
        status, code = registry.start(
            request.analysisId,
            lambda control: execute_workbook_analysis(prepared, provider, store, runner, control),
            stage="preparing_answer",
            session_id=session_id,
            resume_planned=True,
        )
        return _status(status, code)

    @app.get("/api/analyses/{analysis_id}")
    def analysis_status(analysis_id: str = AnalysisIdPath):
        status = registry.status(analysis_id)
        if status is None:
            result = FailedAnalysisResult(analysisId=analysis_id, status="failed", elapsedMs=0, error=AnalysisError(code="ANALYSIS_NOT_FOUND", message="Состояние анализа больше недоступно: он завершён давно или FinLens API был перезапущен.", nextAction="Отправьте вопрос ещё раз."))
            return _failed_status(result)
        return _status(status)

    @app.post("/api/analyses/{analysis_id}/cancel")
    def cancel_analysis(analysis_id: str = AnalysisIdPath):
        return _status(registry.cancel(analysis_id))

    return app


app = create_app()
