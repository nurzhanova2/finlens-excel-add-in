from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from app.domain.contracts import Narrative, RangeSelection, Scope, Source, TabularArtifact, WorkbookMap

logger = logging.getLogger(__name__)

DEFAULT_SESSION_TTL_SECONDS = 1_800
MIN_SESSION_TTL_SECONDS = 60
MAX_SESSION_TTL_SECONDS = 86_400
MAX_SESSIONS = 500
MAX_STORED_TURNS = 20
MAX_PENDING_PLANS = 3


def load_session_ttl_seconds() -> int:
    raw = os.getenv("FINLENS_SESSION_TTL_SECONDS")
    if raw is None:
        return DEFAULT_SESSION_TTL_SECONDS
    try:
        ttl = int(raw)
    except ValueError:
        ttl = 0
    if not MIN_SESSION_TTL_SECONDS <= ttl <= MAX_SESSION_TTL_SECONDS:
        logger.warning("session_ttl_invalid fallbackSeconds=%s", DEFAULT_SESSION_TTL_SECONDS)
        return DEFAULT_SESSION_TTL_SECONDS
    return ttl


class SessionNotFound(Exception):
    pass


class SessionCapacityExceeded(Exception):
    pass


@dataclass(frozen=True)
class ConversationTurn:
    question: str
    narrative: Narrative
    sources: tuple[Source, ...]


@dataclass(frozen=True)
class PendingPlan:
    question: str
    selections: tuple[RangeSelection, ...]


@dataclass
class SessionState:
    """Everything a session holds lives here, in process memory only."""

    session_id: str
    scope: Scope
    expires_at: float
    workbook_map: WorkbookMap | None = None
    history: list[ConversationTurn] = field(default_factory=list)
    pending_plans: dict[str, PendingPlan] = field(default_factory=dict)
    artifacts: list[TabularArtifact] = field(default_factory=list)


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: str
    scope: Scope
    workbook_map: WorkbookMap | None
    history: tuple[ConversationTurn, ...]


class SessionStore:
    """Process-local session memory with idle TTL. No persistence by design (AD-6)."""

    def __init__(self, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS, max_sessions: int = MAX_SESSIONS, clock: Callable[[], float] = time.monotonic):
        self.ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._clock = clock
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def create(self, scope: Scope) -> str:
        with self._lock:
            self._purge_expired_locked()
            if len(self._sessions) >= self._max_sessions:
                raise SessionCapacityExceeded()
            # Opaque random ID: never derived from the workbook name or cell content.
            session_id = secrets.token_urlsafe(32)
            self._sessions[session_id] = SessionState(session_id, scope, self._clock() + self.ttl_seconds)
            return session_id

    def snapshot(self, session_id: str) -> SessionSnapshot:
        with self._lock:
            session = self._touch_locked(session_id)
            return SessionSnapshot(session.session_id, session.scope, session.workbook_map, tuple(session.history))

    def set_workbook_map(self, session_id: str, workbook_map: WorkbookMap) -> None:
        with self._lock:
            self._touch_locked(session_id).workbook_map = workbook_map

    def add_pending_plan(self, session_id: str, analysis_id: str, plan: PendingPlan) -> None:
        with self._lock:
            plans = self._touch_locked(session_id).pending_plans
            plans[analysis_id] = plan
            while len(plans) > MAX_PENDING_PLANS:
                plans.pop(next(iter(plans)))

    def pop_pending_plan(self, session_id: str, analysis_id: str) -> PendingPlan | None:
        with self._lock:
            return self._touch_locked(session_id).pending_plans.pop(analysis_id, None)

    def discard_pending_plan(self, session_id: str, analysis_id: str) -> None:
        """Cleanup after cancellation: drops the plan without extending the session TTL; a closed session is fine."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.pending_plans.pop(analysis_id, None)

    def record_turn(self, session_id: str, turn: ConversationTurn, artifacts: list[TabularArtifact]) -> None:
        with self._lock:
            session = self._touch_locked(session_id)
            session.history.append(turn)
            del session.history[:-MAX_STORED_TURNS]
            # Only the artifacts of the latest turn are kept as temporary session data.
            session.artifacts = list(artifacts)

    def close(self, session_id: str) -> bool:
        with self._lock:
            return self._discard_locked(session_id)

    def purge_expired(self) -> int:
        with self._lock:
            return self._purge_expired_locked()

    def _touch_locked(self, session_id: str) -> SessionState:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFound()
        now = self._clock()
        if session.expires_at <= now:
            self._discard_locked(session_id)
            raise SessionNotFound()
        session.expires_at = now + self.ttl_seconds
        return session

    def _discard_locked(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        session.workbook_map = None
        session.history.clear()
        session.pending_plans.clear()
        session.artifacts.clear()
        return True

    def _purge_expired_locked(self) -> int:
        now = self._clock()
        expired = [session_id for session_id, session in self._sessions.items() if session.expires_at <= now]
        for session_id in expired:
            self._discard_locked(session_id)
        if expired:
            logger.info("sessions_expired count=%s", len(expired))
        return len(expired)
