"""Analysis lifecycle (Story 3.1): typed progress, real cancellation and an in-memory job registry.

An analysis runs in a background worker after its request was validated. The add-in polls a typed
`AnalysisStatus` and may cancel at any time. Cancellation is cooperative but real:

- every Qwen call receives the cancel signal; on cancel the HTTP adapter shuts its socket down, so the
  in-flight request is aborted (not left running), and the worker waits briefly for the call to unwind
  before it reports `cancelled`; no further model requests are made and no late answer is used;
- the sandbox adapter watches the same signal and force-removes the running container;
- the only point where an analysis becomes visible to the session (`commit`) is atomic with the cancel
  check, so a cancelled analysis can never be recorded or returned as completed;
- on cancellation the job drops its inputs and outputs and runs the cleanup hooks (pending workbook plan).

Everything lives in process memory only (AD-6): no database, files or persistent queue.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from app.application.errors import AnalysisFailure
from app.domain.cancellation import CallCancelled, CancelSignal
from app.domain.contracts import (
    TERMINAL_STAGES,
    AnalysisError,
    AnalysisResult,
    AnalysisStatus,
    CancelledAnalysisResult,
    CompletedAnalysisResult,
    FailedAnalysisResult,
    WorkbookPlan,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")
WorkOutcome = CompletedAnalysisResult | FailedAnalysisResult | WorkbookPlan

DEFAULT_ANALYSIS_TIMEOUT_SECONDS = 600
DEFAULT_MAX_ACTIVE_ANALYSES = 8
# Finished jobs stay pollable for a short while (lost poll responses), then they are dropped from memory.
RESULT_RETENTION_SECONDS = 300
MAX_TRACKED_JOBS = 256
CANCEL_WAIT_SECONDS = 20
# After a cancel the worker waits this long for an aborted Qwen call to unwind (socket closed) before it moves on.
MODEL_CALL_ABORT_GRACE_SECONDS = 2.0
_POLL_SECONDS = 0.05

CANCELLED_MESSAGE = "Анализ отменён. Временные данные запроса удалены, ответ не сформирован."
CANCELLED_NEXT_ACTION = "Задайте новый вопрос."


class AnalysisCancelled(Exception):
    """Raised inside a worker once its analysis was cancelled; nothing after it may become visible."""


class AnalysisControl:
    """What a running analysis may do with its own lifecycle. This base is used outside the job registry."""

    def __init__(self) -> None:
        self.cancel_event = CancelSignal()

    def report(self, stage: str, attempt: int | None = None) -> None:
        pass

    def checkpoint(self) -> None:
        if self.cancel_event.is_set():
            raise AnalysisCancelled()

    def call(self, fn: Callable[..., T], *args) -> T:
        """Run an external call (Qwen) that accepts `cancel=`; once cancelled, its outcome is never used."""
        self.checkpoint()
        try:
            value = fn(*args, cancel=self.cancel_event)
        except CallCancelled:
            raise AnalysisCancelled() from None
        except Exception:
            self.checkpoint()
            raise
        self.checkpoint()
        return value

    def commit(self, publish: Callable[[], None] | None = None) -> None:
        """The single point where the result becomes visible (session history); atomic with cancellation."""
        self.checkpoint()
        if publish:
            publish()


@dataclass
class _Job:
    analysis_id: str
    session_id: str | None
    stage: str
    started: float
    attempt: int | None = None
    plan: WorkbookPlan | None = None
    result: AnalysisResult | None = None
    control: "_JobControl | None" = None
    worker_done: threading.Event | None = None
    committed: bool = False
    cancel_reason: str | None = None
    on_cancel: list[Callable[[], None]] = field(default_factory=list)
    finished_at: float | None = None
    timer: threading.Timer | None = None

    @property
    def terminal(self) -> bool:
        return self.stage in TERMINAL_STAGES


class _JobControl(AnalysisControl):
    def __init__(self, jobs: "AnalysisJobs", job: _Job) -> None:
        super().__init__()
        self._jobs, self._job = jobs, job

    def report(self, stage: str, attempt: int | None = None) -> None:
        self._jobs._report(self._job, self, stage, attempt)

    def call(self, fn: Callable[..., T], *args) -> T:
        self.checkpoint()
        box: dict = {}
        finished = threading.Event()

        def target() -> None:
            try:
                box["value"] = fn(*args, cancel=self.cancel_event)
            except BaseException as error:  # re-raised in the worker below
                box["error"] = error
            finally:
                finished.set()

        # The call runs in a helper thread so the worker reacts to a cancel at once even if a provider
        # ignores the signal. The HTTP provider does honour it: the cancel closes its socket, and the
        # worker waits (bounded) until the request has really unwound before reporting `cancelled`.
        threading.Thread(target=target, name="finlens-model-call", daemon=True).start()
        while not finished.wait(_POLL_SECONDS) and not self.cancel_event.is_set():
            pass
        if self.cancel_event.is_set():
            self._await_abort(finished)
            raise AnalysisCancelled()
        if "error" in box:
            if isinstance(box["error"], CallCancelled):
                raise AnalysisCancelled()
            raise box["error"]
        return box["value"]

    def _await_abort(self, finished: threading.Event) -> None:
        started = time.monotonic()
        unwound = finished.wait(self._jobs.model_abort_grace_seconds)
        waited_ms = round((time.monotonic() - started) * 1000)
        if unwound:
            logger.info("model_call_aborted analysisId=%s unwindMs=%s", self._job.analysis_id, waited_ms)
        else:
            # The provider ignored the signal; its late reply is still discarded with the helper thread.
            logger.warning("model_call_abandoned analysisId=%s waitedMs=%s", self._job.analysis_id, waited_ms)

    def commit(self, publish: Callable[[], None] | None = None) -> None:
        self._jobs._commit(self._job, self, publish)


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, default))
    except ValueError:
        return default
    return value if low <= value <= high else default


def _elapsed_ms(started: float, now: float) -> int:
    return max(0, round((now - started) * 1000))


class AnalysisJobs:
    """Process-local registry of analyses keyed by the opaque `analysisId`."""

    def __init__(
        self,
        max_active: int = DEFAULT_MAX_ACTIVE_ANALYSES,
        timeout_seconds: float = DEFAULT_ANALYSIS_TIMEOUT_SECONDS,
        retention_seconds: float = RESULT_RETENTION_SECONDS,
        max_tracked: int = MAX_TRACKED_JOBS,
        cancel_wait_seconds: float = CANCEL_WAIT_SECONDS,
        model_abort_grace_seconds: float = MODEL_CALL_ABORT_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_active = max_active
        self.timeout_seconds = timeout_seconds
        self.retention_seconds = retention_seconds
        self.max_tracked = max_tracked
        self.cancel_wait_seconds = cancel_wait_seconds
        self.model_abort_grace_seconds = model_abort_grace_seconds
        self._clock = clock
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "AnalysisJobs":
        return cls(
            max_active=_int_env("FINLENS_MAX_ACTIVE_ANALYSES", DEFAULT_MAX_ACTIVE_ANALYSES, 1, 64),
            timeout_seconds=_int_env("FINLENS_ANALYSIS_TIMEOUT_SECONDS", DEFAULT_ANALYSIS_TIMEOUT_SECONDS, 60, 3_600),
        )

    # ---- public API -------------------------------------------------------------------------

    def status(self, analysis_id: str) -> AnalysisStatus | None:
        with self._lock:
            self._prune_locked()
            job = self._jobs.get(analysis_id)
            return self._snapshot_locked(job) if job else None

    def start(
        self,
        analysis_id: str,
        work: Callable[[AnalysisControl], WorkOutcome],
        *,
        stage: str,
        session_id: str | None = None,
        on_cancel: Callable[[], None] | None = None,
        resume_planned: bool = False,
    ) -> tuple[AnalysisStatus, int]:
        """Start (or, for workbook phase 2, resume) an analysis. Returns the snapshot and an HTTP status."""
        with self._lock:
            self._prune_locked()
            job = self._jobs.get(analysis_id)
            if job is not None:
                # A cancelled or finished analysis is never restarted under the same ID (cancel-before-start race).
                if job.terminal:
                    return self._snapshot_locked(job), 200
                if not (resume_planned and job.stage == "planned"):
                    return self._failed_snapshot(analysis_id, "ANALYSIS_CONFLICT", "Этот анализ уже выполняется.", "Дождитесь результата текущего анализа."), 409
            if self._active_locked() >= self.max_active:
                logger.warning("analysis_rejected analysisId=%s code=ANALYSIS_CAPACITY", analysis_id)
                return self._failed_snapshot(analysis_id, "ANALYSIS_CAPACITY", "FinLens сейчас выполняет слишком много анализов.", "Повторите вопрос через минуту."), 503
            if job is None:
                job = _Job(analysis_id=analysis_id, session_id=session_id, stage=stage, started=self._clock())
                self._jobs[analysis_id] = job
                job.timer = threading.Timer(self.timeout_seconds, self._expire, args=(analysis_id,))
                job.timer.daemon = True
                job.timer.start()
            job.stage, job.plan, job.attempt = stage, None, None
            job.on_cancel = [on_cancel] if on_cancel else []
            control = _JobControl(self, job)
            job.control, job.worker_done = control, threading.Event()
            snapshot = self._snapshot_locked(job)
        threading.Thread(target=self._run, args=(job, control, work), name="finlens-analysis", daemon=True).start()
        return snapshot, 202

    def cancel(self, analysis_id: str, reason: str = "user", wait: bool = True) -> AnalysisStatus:
        """Cancel an analysis and report its state once cleanup finished (or the wait elapsed)."""
        hooks: list[Callable[[], None]] = []
        done: threading.Event | None = None
        with self._lock:
            self._prune_locked()
            job = self._jobs.get(analysis_id)
            if job is None:
                # Cancel may overtake the start request: remember it so a late start never runs.
                job = _Job(analysis_id=analysis_id, session_id=None, stage="cancelled", started=self._clock())
                job.result = self._cancelled_result(job)
                job.finished_at = self._clock()
                if len(self._jobs) < self.max_tracked:
                    self._jobs[analysis_id] = job
                return self._snapshot_locked(job)
            if job.terminal or job.committed or job.cancel_reason:
                done = job.worker_done if job.cancel_reason else None
            elif job.control is None:
                # Workbook phase 1 finished and the add-in has not sent artifacts yet: no worker to stop.
                job.cancel_reason = reason
                hooks = self._to_cancelled_locked(job)
            else:
                job.cancel_reason = reason
                job.stage = "cancelling"
                job.control.cancel_event.set()
                done = job.worker_done
        self._run_hooks(analysis_id, hooks)
        if wait and done is not None:
            done.wait(self.cancel_wait_seconds)
        with self._lock:
            return self._snapshot_locked(job)

    def abandon_planned(self, analysis_id: str, result: FailedAnalysisResult) -> None:
        """Finish a planned analysis whose phase 2 was rejected during validation."""
        hooks: list[Callable[[], None]] = []
        with self._lock:
            job = self._jobs.get(analysis_id)
            if job is None or job.stage != "planned" or job.control is not None:
                return
            hooks = job.on_cancel
            job.plan, job.result, job.stage = None, result, "failed"
            self._close_locked(job)
        self._run_hooks(analysis_id, hooks)

    def cancel_session(self, session_id: str) -> int:
        """Stop every unfinished analysis of a closed session (scope change, new dialog, pane unload)."""
        with self._lock:
            ids = [job.analysis_id for job in self._jobs.values() if job.session_id == session_id and not job.terminal]
        for analysis_id in ids:
            self.cancel(analysis_id, wait=False)
        return len(ids)

    def active_count(self) -> int:
        with self._lock:
            return self._active_locked()

    def __len__(self) -> int:
        with self._lock:
            return len(self._jobs)

    # ---- worker side ------------------------------------------------------------------------

    def _run(self, job: _Job, control: _JobControl, work: Callable[[AnalysisControl], WorkOutcome]) -> None:
        outcome: WorkOutcome | None
        try:
            outcome = work(control)
        except AnalysisCancelled:
            outcome = None
        except AnalysisFailure as failure:
            outcome = FailedAnalysisResult(analysisId=job.analysis_id, status="failed", error=AnalysisError(code=failure.code, message=failure.message, nextAction=failure.next_action), computationAttempts=failure.attempts, elapsedMs=0)
        except Exception as error:  # never leak the message: it may contain workbook data
            logger.error("analysis_internal_error analysisId=%s errorType=%s", job.analysis_id, type(error).__name__)
            outcome = FailedAnalysisResult(analysisId=job.analysis_id, status="failed", error=AnalysisError(code="INTERNAL_ERROR", message="Во время анализа произошла внутренняя ошибка FinLens.", nextAction="Повторите вопрос."), elapsedMs=0)
        del work  # drop the request closure (artifacts, question) as soon as the worker is done
        self._finish(job, control, outcome)

    def _report(self, job: _Job, control: _JobControl, stage: str, attempt: int | None) -> None:
        with self._lock:
            if job.control is control and not control.cancel_event.is_set() and not job.committed:
                job.stage, job.attempt = stage, attempt

    def _commit(self, job: _Job, control: _JobControl, publish: Callable[[], None] | None) -> None:
        with self._lock:
            if control.cancel_event.is_set() or job.control is not control:
                raise AnalysisCancelled()
            # Session state is published under the registry lock: a cancel either wins before this
            # point (nothing recorded) or arrives after it and is refused (the analysis completed).
            if publish:
                publish()
            job.committed = True
            job.stage, job.attempt = "finalizing", None

    def _finish(self, job: _Job, control: _JobControl, outcome: WorkOutcome | None) -> None:
        hooks: list[Callable[[], None]] = []
        with self._lock:
            done = job.worker_done
            if job.control is control:
                job.control = None
                if control.cancel_event.is_set() and not job.committed:
                    hooks = self._to_cancelled_locked(job)
                elif isinstance(outcome, WorkbookPlan):
                    job.stage, job.plan, job.attempt = "planned", outcome, None
                elif outcome is not None:
                    job.result = outcome.model_copy(update={"elapsedMs": _elapsed_ms(job.started, self._clock())})
                    job.stage, job.attempt = outcome.status, None
                    self._close_locked(job)
                else:
                    # Cancelled from inside without a registry cancel (should not happen): treat as cancelled.
                    job.cancel_reason = job.cancel_reason or "user"
                    hooks = self._to_cancelled_locked(job)
        self._run_hooks(job.analysis_id, hooks)
        if done is not None:
            done.set()

    def _expire(self, analysis_id: str) -> None:
        self.cancel(analysis_id, reason="timeout", wait=False)

    # ---- helpers (lock held) ----------------------------------------------------------------

    def _to_cancelled_locked(self, job: _Job) -> list[Callable[[], None]]:
        hooks = job.on_cancel
        job.on_cancel = []
        job.plan = None
        job.attempt = None
        if job.cancel_reason == "timeout":
            job.stage = "failed"
            job.result = FailedAnalysisResult(analysisId=job.analysis_id, status="failed", error=AnalysisError(code="ANALYSIS_TIMEOUT", message="Анализ занял больше допустимого времени и был остановлен. Временные данные запроса удалены.", nextAction="Выделите меньший диапазон и отправьте вопрос ещё раз."), elapsedMs=_elapsed_ms(job.started, self._clock()))
        else:
            job.stage = "cancelled"
            job.result = self._cancelled_result(job)
        logger.info("analysis_cancelled analysisId=%s reason=%s elapsedMs=%s", job.analysis_id, job.cancel_reason, job.result.elapsedMs)
        self._close_locked(job)
        return hooks

    def _close_locked(self, job: _Job) -> None:
        job.finished_at = self._clock()
        job.on_cancel = []
        if job.timer is not None:
            job.timer.cancel()
            job.timer = None

    def _cancelled_result(self, job: _Job) -> CancelledAnalysisResult:
        return CancelledAnalysisResult(analysisId=job.analysis_id, status="cancelled", message=CANCELLED_MESSAGE, nextAction=CANCELLED_NEXT_ACTION, elapsedMs=_elapsed_ms(job.started, self._clock()))

    def _failed_snapshot(self, analysis_id: str, code: str, message: str, next_action: str) -> AnalysisStatus:
        result = FailedAnalysisResult(analysisId=analysis_id, status="failed", error=AnalysisError(code=code, message=message, nextAction=next_action), elapsedMs=0)
        return AnalysisStatus(analysisId=analysis_id, stage="failed", elapsedMs=0, result=result)

    def _snapshot_locked(self, job: _Job) -> AnalysisStatus:
        elapsed = job.result.elapsedMs if job.result is not None else _elapsed_ms(job.started, self._clock())
        return AnalysisStatus(analysisId=job.analysis_id, stage=job.stage, computationAttempt=job.attempt, elapsedMs=elapsed, plan=job.plan if job.stage == "planned" else None, result=job.result if job.terminal else None)

    def _active_locked(self) -> int:
        return sum(1 for job in self._jobs.values() if job.control is not None)

    def _prune_locked(self) -> None:
        now = self._clock()
        expired = [key for key, job in self._jobs.items() if job.finished_at is not None and now - job.finished_at > self.retention_seconds]
        for key in expired:
            del self._jobs[key]
        overflow = len(self._jobs) - self.max_tracked
        if overflow > 0:
            finished = sorted((job.finished_at, key) for key, job in self._jobs.items() if job.finished_at is not None)
            for _, key in finished[:overflow]:
                del self._jobs[key]

    @staticmethod
    def _run_hooks(analysis_id: str, hooks: list[Callable[[], None]]) -> None:
        for hook in hooks:
            try:
                hook()
            except Exception as error:
                logger.warning("analysis_cleanup_failed analysisId=%s errorType=%s", analysis_id, type(error).__name__)
