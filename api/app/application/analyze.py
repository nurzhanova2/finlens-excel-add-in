from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.adapters.qwen import AnswerContext, QwenProvider, QwenProviderError
from app.adapters.sandbox import SandboxRunner, UnavailableSandboxRunner
from app.application.agent import AgentOutcome, run_agent
from app.application.errors import AnalysisFailure
from app.application.lifecycle import AnalysisControl
from app.application.sessions import ConversationTurn, SessionNotFound, SessionStore
from app.domain.contracts import AnalysisError, AnalysisRequest, CompletedAnalysisResult, FailedAnalysisResult, Source

logger = logging.getLogger(__name__)

__all__ = ["AnalysisFailure", "PreparedAnalysis", "analyze", "computation_log_fields", "elapsed_ms", "execute_analysis", "failed", "prepare_analysis", "session_not_found", "session_scope_mismatch"]


def session_not_found() -> AnalysisFailure:
    return AnalysisFailure("SESSION_NOT_FOUND", "Сеанс FinLens завершён или истёк, история диалога очищена.", "Отправьте вопрос ещё раз: начнётся новый сеанс.")


def session_scope_mismatch() -> AnalysisFailure:
    return AnalysisFailure("SESSION_SCOPE_MISMATCH", "Сеанс относится к другому контексту Excel.", "Отправьте вопрос ещё раз: для текущего контекста начнётся новый сеанс.")


def elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def failed(analysis_id: str, started: float, error: AnalysisFailure | QwenProviderError) -> FailedAnalysisResult:
    attempts = getattr(error, "attempts", None)
    result = FailedAnalysisResult(analysisId=analysis_id, status="failed", error=AnalysisError(code=error.code, message=error.message, nextAction=error.next_action), computationAttempts=attempts, elapsedMs=elapsed_ms(started))
    logger.warning("analysis_failed analysisId=%s elapsedMs=%s code=%s computationAttempts=%s", analysis_id, result.elapsedMs, error.code, attempts)
    return result


def computation_log_fields(outcome: AgentOutcome) -> str:
    computation = outcome.computation
    return f"computed={computation is not None} attempts={computation.attempts if computation else 0}"


@dataclass(frozen=True)
class PreparedAnalysis:
    """A validated range/sheet request; nothing reaches Qwen or the sandbox before this exists."""

    request: AnalysisRequest
    history: tuple[ConversationTurn, ...]
    started: float


def prepare_analysis(request: AnalysisRequest, sessions: SessionStore | None = None) -> PreparedAnalysis:
    started = time.perf_counter()
    history: tuple[ConversationTurn, ...] = ()
    if request.sessionId and sessions is not None:
        try:
            snapshot = sessions.snapshot(request.sessionId)
        except SessionNotFound:
            raise session_not_found()
        if snapshot.scope != request.scope:
            raise session_scope_mismatch()
        history = snapshot.history
    return PreparedAnalysis(request, history, started)


def execute_analysis(prepared: PreparedAnalysis, provider: QwenProvider, sessions: SessionStore | None = None, sandbox: SandboxRunner | None = None, control: AnalysisControl | None = None) -> CompletedAnalysisResult | FailedAnalysisResult:
    control = control or AnalysisControl()
    request, started = prepared.request, prepared.started
    try:
        context = AnswerContext(analysis_id=request.analysisId, scope=request.scope, question=request.question, artifacts=(request.artifact,), history=prepared.history)
        outcome = run_agent(context, provider, sandbox or UnavailableSandboxRunner(), control)
        narrative = outcome.narrative
        sources = [Source(sheetName=request.artifact.sheetName, address=request.artifact.address)]
        result = CompletedAnalysisResult(analysisId=request.analysisId, status="completed", narrative=narrative, sources=sources, computation=outcome.computation, elapsedMs=elapsed_ms(started))

        def publish() -> None:
            if request.sessionId and sessions is not None:
                sessions.record_turn(request.sessionId, ConversationTurn(request.question, narrative, tuple(sources)), [request.artifact])

        try:
            control.commit(publish)
        except SessionNotFound:
            raise session_not_found()
        logger.info("analysis_completed analysisId=%s elapsedMs=%s cells=%s turns=%s insufficientData=%s %s", result.analysisId, result.elapsedMs, request.artifact.cell_count, len(prepared.history), narrative.insufficientData, computation_log_fields(outcome))
        return result
    except (AnalysisFailure, QwenProviderError) as error:
        return failed(request.analysisId, started, error)


def analyze(request: AnalysisRequest, provider: QwenProvider, sessions: SessionStore | None = None, sandbox: SandboxRunner | None = None, control: AnalysisControl | None = None) -> CompletedAnalysisResult | FailedAnalysisResult:
    """Synchronous use case (prepare + execute) for callers outside the job registry."""
    started = time.perf_counter()
    try:
        prepared = prepare_analysis(request, sessions)
    except AnalysisFailure as error:
        return failed(request.analysisId, started, error)
    return execute_analysis(prepared, provider, sessions, sandbox, control)
