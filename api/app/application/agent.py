"""Qwen tool-calling loop with execution truth before narrative (AD-3, AD-5).

The model may request only `run_analysis`. Every request is validated (tool name, strict schema,
artifact references of the current analysis only, size, code policy) before a fresh sandbox job is
created. A failed attempt returns only a bounded diagnostic to the model; after the first run and two
corrections the analysis fails honestly. A computed answer is accepted only after a successful job.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from pydantic import ValidationError

from app.adapters.qwen import AnswerContext, ModelTurn, QwenProvider, RawToolCall, ToolExchange
from app.adapters.sandbox import ExecutionFailed, ExecutionSucceeded, SandboxBusy, SandboxCancelled, SandboxInput, SandboxJob, SandboxRunner, SandboxUnavailable
from app.application.errors import AnalysisFailure
from app.application.lifecycle import AnalysisCancelled, AnalysisControl
from app.domain.code_policy import check_code, check_text
from app.domain.computation import (
    MAX_DIAGNOSTIC_CHARS,
    MAX_EXECUTION_ATTEMPTS,
    TOOL_NAME,
    Computation,
    ComputationInput,
    RunAnalysisArguments,
    SafeDiagnostic,
    artifact_ref,
)
from app.domain.contracts import ModelAnswer, Narrative, TabularArtifact

logger = logging.getLogger(__name__)

# Rows of each result table echoed back to the model; the analyst sees the full bounded table.
MAX_MODEL_TABLE_ROWS = 50
_PATH = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][\w.@+-]+){2,}[\\/]?")
_SECRETISH = re.compile(r"(?i)(FINLENS_\w+|QWEN_\w+|bearer\s+\S+|api[_-]?key\S*)")


@dataclass(frozen=True)
class AgentOutcome:
    narrative: Narrative
    computation: Computation | None


@dataclass(frozen=True)
class _Accepted:
    arguments: RunAnalysisArguments
    inputs: tuple[SandboxInput, ...]


def sanitize_diagnostic(diagnostic: SafeDiagnostic) -> SafeDiagnostic:
    """Second, host-side scrub: no host paths, secrets or long traces ever reach the model."""
    message = _SECRETISH.sub("<redacted>", _PATH.sub("<path>", diagnostic.message))
    return SafeDiagnostic(code=diagnostic.code, errorType=diagnostic.errorType, message=" ".join(message.split())[:MAX_DIAGNOSTIC_CHARS], line=diagnostic.line)


def _rejected(reason: str, line: int | None = None) -> SafeDiagnostic:
    return SafeDiagnostic(code="TOOL_CALL_REJECTED", errorType="PolicyViolation", message=reason[:MAX_DIAGNOSTIC_CHARS], line=line)


def validate_tool_call(calls: tuple[RawToolCall, ...], artifacts: dict[str, TabularArtifact]) -> _Accepted | SafeDiagnostic:
    """Validate a model tool call; nothing invalid ever reaches the sandbox."""
    if len(calls) != 1:
        return _rejected("exactly one run_analysis call per turn is allowed")
    call = calls[0]
    if call.name != TOOL_NAME:
        return _rejected(f"unknown tool; the only available tool is {TOOL_NAME}")
    try:
        arguments = RunAnalysisArguments.model_validate_json(call.arguments)
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(str(part) for part in item['loc']) or 'arguments'}: {item['type']}" for item in error.errors()[:5])
        return _rejected(f"arguments do not match the run_analysis schema ({problems})")
    unknown = [ref for ref in arguments.inputs if ref not in artifacts]
    if unknown:
        return _rejected(f"inputs reference artifacts that are not part of this analysis; available: {', '.join(artifacts)}")
    violation = check_text(arguments.method, "method") or check_code(arguments.code)
    if violation:
        return _rejected(violation.reason, violation.line)
    return _Accepted(arguments, tuple(SandboxInput(ref, artifacts[ref]) for ref in arguments.inputs))


def _inputs_summary(artifacts: dict[str, TabularArtifact]) -> list[dict]:
    return [{"ref": ref, "sheetName": a.sheetName, "address": a.address, "rows": len(a.values), "columns": len(a.values[0])} for ref, a in artifacts.items()]


def _failure_result(attempt: int, diagnostic: SafeDiagnostic, artifacts: dict[str, TabularArtifact]) -> dict:
    return {
        "status": "failed",
        "attempt": attempt,
        "attemptsRemaining": MAX_EXECUTION_ATTEMPTS - attempt,
        "error": diagnostic.model_dump(),
        "availableInputs": _inputs_summary(artifacts),
    }


def _success_result(attempt: int, computation: Computation) -> dict:
    return {
        "status": "succeeded",
        "attempt": attempt,
        "tables": [{"title": t.title, "columns": t.columns, "rows": t.rows[:MAX_MODEL_TABLE_ROWS], "totalRows": t.totalRows} for t in computation.tables],
        "charts": [{"title": c.title, "description": c.description.model_dump()} for c in computation.charts],
        "stdout": computation.stdout,
    }


def parse_model_answer(content: str | None) -> ModelAnswer:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    try:
        return ModelAnswer.model_validate_json(text)
    except ValidationError as error:
        raise AnalysisFailure("QWEN_INVALID_RESPONSE", "Внутренний Qwen вернул ответ в недопустимом формате.", "Сформулируйте вопрос точнее и отправьте его ещё раз.") from error


def computation_failed(attempts: int) -> AnalysisFailure:
    return AnalysisFailure(
        "COMPUTATION_FAILED",
        f"Расчёт не удалось выполнить после {attempts} попыток (исходный запуск и {attempts - 1} автоматических исправления). Числа и выводы не показаны, чтобы не выдавать непроверенный результат за расчёт.",
        "Выделите меньший диапазон с нужными данными и повторите вопрос.",
        attempts=attempts,
    )


def _model_stage(attempts: int, succeeded: Computation | None) -> tuple[str, int | None]:
    """While the model corrects a failed run the analyst sees «Исправляю расчёт», otherwise «Готовлю вывод»."""
    if attempts and succeeded is None and attempts < MAX_EXECUTION_ATTEMPTS:
        return "retrying_computation", attempts + 1
    return "preparing_answer", None


def run_agent(context: AnswerContext, provider: QwenProvider, sandbox: SandboxRunner, control: AnalysisControl | None = None) -> AgentOutcome:
    control = control or AnalysisControl()
    artifacts = {artifact_ref(index): artifact for index, artifact in enumerate(context.artifacts)}
    exchanges: list[ToolExchange] = []
    attempts = 0
    succeeded: Computation | None = None
    while True:
        tools_enabled = succeeded is None and attempts < MAX_EXECUTION_ATTEMPTS
        control.report(*_model_stage(attempts, succeeded))
        # Cancellation stops any further Qwen request; an in-flight one is abandoned by the control.
        turn: ModelTurn = control.call(provider.next_turn, context, tuple(exchanges), tools_enabled)
        if turn.tool_calls:
            if not tools_enabled:
                raise AnalysisFailure("QWEN_INVALID_RESPONSE", "Внутренний Qwen запросил расчёт, когда он уже недоступен.", "Сформулируйте вопрос точнее и отправьте его ещё раз.", attempts=attempts or None)
            attempts += 1
            control.report("running_computation" if attempts == 1 else "retrying_computation", attempts)
            result, succeeded = _attempt(context, turn.tool_calls, artifacts, sandbox, attempts, control)
            control.checkpoint()
            if succeeded is None and attempts >= MAX_EXECUTION_ATTEMPTS:
                raise computation_failed(attempts)
            exchanges.append(ToolExchange(turn.tool_calls, result))
            continue
        answer = parse_model_answer(turn.content)
        if answer.basedOnComputation and succeeded is None:
            # AD-5: a computed claim without a successful execution artifact is never shown.
            logger.warning("unverified_computation analysisId=%s attempts=%s", context.analysis_id, attempts)
            raise AnalysisFailure(
                "UNVERIFIED_COMPUTATION",
                "Ответ ссылался на расчёт, который не был успешно выполнен, поэтому он не показан.",
                "Уточните, какой расчёт нужен, и отправьте вопрос ещё раз.",
                attempts=attempts or None,
            )
        return AgentOutcome(answer.narrative(), succeeded)


def _attempt(context: AnswerContext, calls: tuple[RawToolCall, ...], artifacts: dict[str, TabularArtifact], sandbox: SandboxRunner, attempt: int, control: AnalysisControl) -> tuple[dict, Computation | None]:
    accepted = validate_tool_call(calls, artifacts)
    if isinstance(accepted, SafeDiagnostic):
        logger.info("tool_call_rejected analysisId=%s attempt=%s code=%s", context.analysis_id, attempt, accepted.code)
        return _failure_result(attempt, accepted, artifacts), None
    control.checkpoint()
    job = SandboxJob(analysis_id=context.analysis_id, attempt=attempt, code=accepted.arguments.code, inputs=accepted.inputs, cancel=control.cancel_event)
    try:
        outcome = sandbox.run(job)
    except SandboxCancelled:
        logger.info("computation_attempt analysisId=%s attempt=%s outcome=cancelled", context.analysis_id, attempt)
        raise AnalysisCancelled()
    except SandboxUnavailable:
        logger.warning("sandbox_unavailable analysisId=%s attempt=%s", context.analysis_id, attempt)
        raise AnalysisFailure("SANDBOX_UNAVAILABLE", "Изолированная песочница для расчётов сейчас недоступна, поэтому расчёт не выполнялся.", "Сообщите администратору FinLens, что песочница расчётов недоступна.", attempts=attempt)
    except SandboxBusy:
        raise AnalysisFailure("SANDBOX_BUSY", "Песочница расчётов занята другими анализами.", "Повторите вопрос через минуту.", attempts=attempt)
    cells = sum(item.artifact.cell_count for item in accepted.inputs)
    if isinstance(outcome, ExecutionFailed):
        diagnostic = sanitize_diagnostic(outcome.diagnostic)
        logger.info("computation_attempt analysisId=%s attempt=%s outcome=failed code=%s durationMs=%s inputs=%s cells=%s", context.analysis_id, attempt, diagnostic.code, outcome.duration_ms, len(accepted.inputs), cells)
        return _failure_result(attempt, diagnostic, artifacts), None
    assert isinstance(outcome, ExecutionSucceeded)
    output = outcome.output
    computation = Computation(
        status="succeeded",
        code=accepted.arguments.code,
        method=accepted.arguments.method,
        attempts=attempt,
        inputs=[ComputationInput(sheetName=item.artifact.sheetName, address=item.artifact.address) for item in accepted.inputs],
        tables=output.tables,
        charts=output.charts,
        stdout=output.stdout,
        durationMs=outcome.duration_ms,
    )
    logger.info("computation_attempt analysisId=%s attempt=%s outcome=succeeded durationMs=%s inputs=%s cells=%s tables=%s charts=%s", context.analysis_id, attempt, outcome.duration_ms, len(accepted.inputs), cells, len(output.tables), len(output.charts))
    return _success_result(attempt, computation), computation

