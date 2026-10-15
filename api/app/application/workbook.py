from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.adapters.qwen import AnswerContext, QwenProvider, QwenProviderError, SelectionContext
from app.adapters.sandbox import SandboxRunner, UnavailableSandboxRunner
from app.application.agent import run_agent
from app.application.analyze import computation_log_fields, elapsed_ms, failed, session_not_found, session_scope_mismatch
from app.application.errors import AnalysisFailure
from app.application.lifecycle import AnalysisControl
from app.application.sessions import ConversationTurn, PendingPlan, SessionNotFound, SessionSnapshot, SessionStore
from app.domain.addresses import parse_a1, split_sheet
from app.domain.contracts import (
    MAX_ARTIFACT_CELLS,
    MAX_SELECTED_ARTIFACTS,
    MAX_WORKBOOK_CELLS,
    CompletedAnalysisResult,
    Coverage,
    FailedAnalysisResult,
    RangeSelection,
    Source,
    TabularArtifact,
    WorkbookAnalysisRequest,
    WorkbookLimits,
    WorkbookMap,
    WorkbookPlan,
    WorkbookPlanRequest,
)

logger = logging.getLogger(__name__)

NARROW_ACTION = "Выберите контекст «Лист» или «Выделение» с нужными данными и повторите вопрос."


def _workbook_session(sessions: SessionStore, session_id: str) -> SessionSnapshot:
    try:
        snapshot = sessions.snapshot(session_id)
    except SessionNotFound:
        raise session_not_found()
    if snapshot.scope != "workbook":
        raise session_scope_mismatch()
    return snapshot


def validate_selections(raw: list[RangeSelection], workbook_map: WorkbookMap) -> list[RangeSelection]:
    """Turn model-proposed ranges into bounded, normalized ranges inside known used ranges."""
    selections: list[RangeSelection] = []
    seen: set[tuple[str, str]] = set()
    for proposal in raw:
        sheet = workbook_map.sheet(proposal.sheetName)
        if sheet is None or sheet.usedRangeAddress is None:
            continue
        address_sheet, _ = split_sheet(proposal.address)
        if address_sheet is not None and address_sheet != sheet.sheetName:
            continue
        try:
            requested = parse_a1(proposal.address)
        except ValueError:
            continue
        bounded = requested.intersect(parse_a1(sheet.usedRangeAddress))
        if bounded is None:
            continue
        key = (sheet.sheetName, bounded.to_a1())
        if key in seen:
            continue
        seen.add(key)
        if bounded.cells > MAX_ARTIFACT_CELLS:
            raise AnalysisFailure("WORKBOOK_SCOPE_TOO_LARGE", f"Для ответа нужен диапазон {sheet.sheetName}!{bounded.to_a1()} из {bounded.cells} ячеек, а безопасный лимит — {MAX_ARTIFACT_CELLS} ячеек на диапазон.", NARROW_ACTION)
        selections.append(RangeSelection(sheetName=sheet.sheetName, address=bounded.to_a1(), reason=proposal.reason))
    if not selections:
        raise AnalysisFailure("NO_RELEVANT_DATA", "Не удалось определить данные книги, относящиеся к вопросу.", "Выберите лист с нужными данными (контекст «Лист») и повторите вопрос.")
    if len(selections) > MAX_SELECTED_ARTIFACTS:
        raise AnalysisFailure("WORKBOOK_SCOPE_TOO_LARGE", f"Для ответа нужно больше {MAX_SELECTED_ARTIFACTS} диапазонов книги, это превышает безопасный лимит.", NARROW_ACTION)
    total = sum(parse_a1(selection.address).cells for selection in selections)
    if total > MAX_WORKBOOK_CELLS:
        raise AnalysisFailure("WORKBOOK_SCOPE_TOO_LARGE", f"Релевантные данные занимают {total} ячеек, а безопасный лимит одного анализа — {MAX_WORKBOOK_CELLS} ячеек.", NARROW_ACTION)
    return selections


@dataclass(frozen=True)
class PreparedPlan:
    session_id: str
    request: WorkbookPlanRequest
    workbook_map: WorkbookMap
    history: tuple[ConversationTurn, ...]
    started: float


def prepare_plan(session_id: str, request: WorkbookPlanRequest, sessions: SessionStore) -> PreparedPlan:
    """Phase 1 validation: session, scope and a map with data. Nothing is sent to Qwen if it fails."""
    started = time.perf_counter()
    snapshot = _workbook_session(sessions, session_id)
    workbook_map = request.workbookMap or snapshot.workbook_map
    if workbook_map is None:
        raise AnalysisFailure("WORKBOOK_MAP_MISSING", "Карта книги в этом сеансе недоступна.", "Выберите контекст «Книга» заново и повторите вопрос.")
    if not any(sheet.usedRangeAddress for sheet in workbook_map.sheets):
        raise AnalysisFailure("EMPTY_CONTEXT", "В книге нет листов с данными.", "Откройте книгу с данными.")
    if request.workbookMap is not None:
        try:
            sessions.set_workbook_map(session_id, request.workbookMap)
        except SessionNotFound:
            raise session_not_found()
    return PreparedPlan(session_id, request, workbook_map, snapshot.history, started)


def execute_plan(prepared: PreparedPlan, provider: QwenProvider, sessions: SessionStore, control: AnalysisControl | None = None) -> WorkbookPlan | FailedAnalysisResult:
    control = control or AnalysisControl()
    request, started = prepared.request, prepared.started
    try:
        control.report("selecting_artifacts")
        context = SelectionContext(analysis_id=request.analysisId, question=request.question, workbook_map=prepared.workbook_map, history=prepared.history, limits=WorkbookLimits())
        proposals = control.call(provider.select_ranges, context)
        selections = validate_selections(proposals, prepared.workbook_map)
        control.checkpoint()
        # A cancel after this point removes the pending plan through the job's cleanup hook.
        sessions.add_pending_plan(prepared.session_id, request.analysisId, PendingPlan(request.question, tuple(selections)))
        plan = WorkbookPlan(analysisId=request.analysisId, status="planned", selections=selections, sheetsInWorkbook=len(prepared.workbook_map.sheets), elapsedMs=elapsed_ms(started))
        logger.info("workbook_planned analysisId=%s elapsedMs=%s sheets=%s selections=%s cells=%s", plan.analysisId, plan.elapsedMs, plan.sheetsInWorkbook, len(selections), sum(parse_a1(s.address).cells for s in selections))
        return plan
    except SessionNotFound:
        return failed(request.analysisId, started, session_not_found())
    except (AnalysisFailure, QwenProviderError) as error:
        return failed(request.analysisId, started, error)


def plan_workbook(session_id: str, request: WorkbookPlanRequest, provider: QwenProvider, sessions: SessionStore) -> WorkbookPlan | FailedAnalysisResult:
    started = time.perf_counter()
    try:
        prepared = prepare_plan(session_id, request, sessions)
    except AnalysisFailure as error:
        return failed(request.analysisId, started, error)
    return execute_plan(prepared, provider, sessions)


def workbook_coverage(workbook_map: WorkbookMap, artifacts: list[TabularArtifact]) -> Coverage:
    """Story 3.3: say exactly which part of the workbook was read; complete only if every sheet with data was read in full."""
    with_data = [sheet for sheet in workbook_map.sheets if sheet.usedRangeAddress]
    read = {(artifact.sheetName, parse_a1(artifact.address)) for artifact in artifacts}
    complete = all((sheet.sheetName, parse_a1(sheet.usedRangeAddress)) in read for sheet in with_data)
    sheets_used = list(dict.fromkeys(artifact.sheetName for artifact in artifacts))
    return Coverage(sheetsInWorkbook=len(workbook_map.sheets), sheetsWithData=len(with_data), sheetsUsed=sheets_used, complete=complete)


@dataclass(frozen=True)
class PreparedWorkbookAnalysis:
    session_id: str
    analysis_id: str
    question: str
    artifacts: list[TabularArtifact]
    workbook_map: WorkbookMap
    history: tuple[ConversationTurn, ...]
    started: float


def prepare_workbook_analysis(session_id: str, request: WorkbookAnalysisRequest, sessions: SessionStore) -> PreparedWorkbookAnalysis:
    """Phase 2 validation: only artifacts that exactly match the single-use plan may reach Qwen."""
    started = time.perf_counter()
    snapshot = _workbook_session(sessions, session_id)
    try:
        plan = sessions.pop_pending_plan(session_id, request.analysisId)
    except SessionNotFound:
        raise session_not_found()
    if plan is None or snapshot.workbook_map is None:
        raise AnalysisFailure("PLAN_NOT_FOUND", "План чтения книги для этого вопроса не найден или устарел.", "Отправьте вопрос ещё раз.")
    planned = {(selection.sheetName, parse_a1(selection.address)) for selection in plan.selections}
    received = set()
    for artifact in request.artifacts:
        address_sheet, _ = split_sheet(artifact.address)
        address = parse_a1(artifact.address)
        if address_sheet not in (None, artifact.sheetName) or (address.rows, address.columns) != (len(artifact.values), len(artifact.values[0])):
            raise AnalysisFailure("ARTIFACTS_MISMATCH", "Прочитанные данные не совпадают с планом анализа книги.", "Отправьте вопрос ещё раз.")
        received.add((artifact.sheetName, address))
    # Only exactly the planned ranges may reach Qwen: nothing more, nothing different.
    if received != planned or len(received) != len(request.artifacts):
        raise AnalysisFailure("ARTIFACTS_MISMATCH", "Прочитанные данные не совпадают с планом анализа книги.", "Отправьте вопрос ещё раз.")
    return PreparedWorkbookAnalysis(session_id, request.analysisId, plan.question, list(request.artifacts), snapshot.workbook_map, snapshot.history, started)


def execute_workbook_analysis(prepared: PreparedWorkbookAnalysis, provider: QwenProvider, sessions: SessionStore, sandbox: SandboxRunner | None = None, control: AnalysisControl | None = None) -> CompletedAnalysisResult | FailedAnalysisResult:
    control = control or AnalysisControl()
    started, artifacts = prepared.started, prepared.artifacts
    try:
        context = AnswerContext(analysis_id=prepared.analysis_id, scope="workbook", question=prepared.question, artifacts=tuple(artifacts), history=prepared.history, workbook_map=prepared.workbook_map)
        outcome = run_agent(context, provider, sandbox or UnavailableSandboxRunner(), control)
        narrative = outcome.narrative
        sources = [Source(sheetName=artifact.sheetName, address=artifact.address) for artifact in artifacts]
        result = CompletedAnalysisResult(
            analysisId=prepared.analysis_id,
            status="completed",
            narrative=narrative,
            sources=sources,
            coverage=workbook_coverage(prepared.workbook_map, artifacts),
            computation=outcome.computation,
            elapsedMs=elapsed_ms(started),
        )
        control.commit(lambda: sessions.record_turn(prepared.session_id, ConversationTurn(prepared.question, narrative, tuple(sources)), artifacts))
        logger.info("analysis_completed analysisId=%s elapsedMs=%s artifacts=%s cells=%s turns=%s complete=%s insufficientData=%s %s", result.analysisId, result.elapsedMs, len(artifacts), sum(a.cell_count for a in artifacts), len(prepared.history), result.coverage.complete, narrative.insufficientData, computation_log_fields(outcome))
        return result
    except SessionNotFound:
        return failed(prepared.analysis_id, started, session_not_found())
    except (AnalysisFailure, QwenProviderError) as error:
        return failed(prepared.analysis_id, started, error)


def analyze_workbook(session_id: str, request: WorkbookAnalysisRequest, provider: QwenProvider, sessions: SessionStore, sandbox: SandboxRunner | None = None) -> CompletedAnalysisResult | FailedAnalysisResult:
    started = time.perf_counter()
    try:
        prepared = prepare_workbook_analysis(session_id, request, sessions)
    except AnalysisFailure as error:
        return failed(request.analysisId, started, error)
    return execute_workbook_analysis(prepared, provider, sessions, sandbox)
