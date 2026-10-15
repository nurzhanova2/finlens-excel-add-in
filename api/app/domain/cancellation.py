"""Cancellation signal shared by the analysis lifecycle and the adapters that do blocking I/O.

`CancelSignal` is a `threading.Event` (the sandbox runner polls `is_set()`), plus callbacks that run
once when it is set. The Qwen HTTP adapter registers a callback that closes its socket, so a blocked
request is aborted at the transport level instead of being left to finish in the background.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)


class CallCancelled(Exception):
    """Raised by an adapter whose in-flight call was aborted by its cancel signal."""


class CancelSignal(threading.Event):
    def __init__(self) -> None:
        super().__init__()
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._next_key = 0
        self._callbacks_lock = threading.Lock()

    def set(self) -> None:
        with self._callbacks_lock:
            if self.is_set():
                return
            super().set()
            callbacks = list(self._callbacks.values())
            self._callbacks.clear()
        for callback in callbacks:
            _run(callback)

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Run `callback` once when the signal is set (immediately if it already is). Returns an unregister function."""
        with self._callbacks_lock:
            if not self.is_set():
                key = self._next_key
                self._next_key += 1
                self._callbacks[key] = callback

                def unregister() -> None:
                    with self._callbacks_lock:
                        self._callbacks.pop(key, None)

                return unregister
        _run(callback)
        return lambda: None


def _run(callback: Callable[[], None]) -> None:
    try:
        callback()
    except Exception as error:  # an abort callback must never break cancellation of the analysis
        logger.warning("cancel_callback_failed errorType=%s", type(error).__name__)
