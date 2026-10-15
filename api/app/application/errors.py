from __future__ import annotations


class AnalysisFailure(Exception):
    def __init__(self, code: str, message: str, next_action: str, attempts: int | None = None):
        self.code = code
        self.message = message
        self.next_action = next_action
        # Sandbox attempts made before the failure (Story 2.2); None when no computation was attempted.
        self.attempts = attempts
        super().__init__(message)
