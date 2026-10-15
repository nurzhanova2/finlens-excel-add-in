"""Shared test doubles. ScriptedSandbox is a stand-in for the sandbox port: it never executes code."""
from __future__ import annotations

import base64
import json
import select
import socket
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.adapters.sandbox import ExecutionFailed, ExecutionSucceeded, SandboxJob
from app.domain.computation import RunnerSuccess, SafeDiagnostic

SECRET_CELL = "CONFIDENTIAL-CELL-42"


def tiny_png(width: int = 4, height: int = 3) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def runner_success(stdout: str = "Итого: 220") -> RunnerSuccess:
    return RunnerSuccess.model_validate({
        "status": "succeeded",
        "tables": [{"title": "Итоги", "columns": ["Показатель", "2025"], "rows": [["EBITDA", 220.0], ["Выручка", 1250]], "totalRows": 2, "truncated": False}],
        "charts": [{"title": "EBITDA", "pngBase64": base64.b64encode(tiny_png()).decode(), "widthPx": 4, "heightPx": 3, "description": {"metric": "EBITDA", "period": "2024–2025", "insight": "EBITDA выросла на 10%"}}],
        "stdout": stdout,
        "durationMs": 120,
    })


def succeeded(stdout: str = "Итого: 220") -> ExecutionSucceeded:
    return ExecutionSucceeded(runner_success(stdout), 900)


def failed(code: str = "RUNTIME_ERROR", error_type: str = "KeyError", message: str = "'Revenue'", line: int | None = 3) -> ExecutionFailed:
    return ExecutionFailed(SafeDiagnostic(code=code, errorType=error_type, message=message, line=line), 700)


def artifact(sheet_name: str, address: str, rows: int, columns: int, value=1) -> dict:
    return {"sheetName": sheet_name, "address": address, "values": [[value] * columns for _ in range(rows)], "formulas": [[""] * columns for _ in range(rows)], "numberFormats": [["General"] * columns for _ in range(rows)]}


class ScriptedSandbox:
    """Returns scripted outcomes (or exceptions) in order and records every job it receives."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.jobs: list[SandboxJob] = []

    def run(self, job: SandboxJob):
        self.jobs.append(job)
        outcome = self.outcomes.pop(0) if self.outcomes else succeeded()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class CodeAwareSandbox(ScriptedSandbox):
    """Fails the fake provider's broken/failing code and succeeds otherwise, without executing anything."""

    def run(self, job: SandboxJob):
        self.jobs.append(job)
        if "undefined_totals" in job.code:
            return failed("RUNTIME_ERROR", "NameError", "name 'undefined_totals' is not defined", 2)
        if "NO_SUCH_COLUMN" in job.code:
            return failed("RUNTIME_ERROR", "KeyError", "\"None of [Index(['NO_SUCH_COLUMN'])] are in the [columns]\"", 2)
        return succeeded("Числовых столбцов: 2")


TERMINAL = {"completed", "failed", "cancelled"}


def follow(client, response, until_planned: bool = False, timeout: float = 15.0) -> dict:
    """Follow an analysis start response through GET /api/analyses/{id} like the add-in does.

    Returns the typed result (or the workbook plan for phase 1). A plain failed result (422) is returned as is.
    """
    import time

    body = response.json()
    if "stage" not in body:
        return body
    deadline = time.monotonic() + timeout
    while True:
        if body["stage"] in TERMINAL:
            return body["result"]
        if until_planned and body["stage"] == "planned":
            return body["plan"]
        if time.monotonic() > deadline:
            raise AssertionError(f"analysis did not finish, last stage {body['stage']}")
        time.sleep(0.01)
        body = client.get(f"/api/analyses/{body['analysisId']}").json()


class FakeQwenServer:
    """A real OpenAI-compatible HTTP endpoint on 127.0.0.1 for the HTTP Qwen adapter (no model behind it).

    `replies` are sent in order. A reply of `HOLD` keeps the request open until the test calls `release()`
    or the client closes the connection; `disconnected` is set when the server sees the client go away,
    which is how the tests prove that a cancel really aborted the HTTP request.
    """

    HOLD = object()

    def __init__(self, *replies, status: int = 200):
        self.replies = list(replies)
        self.status = status
        self.requests: list[dict] = []
        self.auth: list[str] = []
        self.received = threading.Event()
        self.held = threading.Event()
        self.disconnected = threading.Event()
        self._release = threading.Event()
        self._held_reply: dict | None = None
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output clean
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                server.requests.append(json.loads(self.rfile.read(length)))
                server.auth.append(self.headers.get("Authorization", ""))
                reply = server.replies.pop(0) if server.replies else server.HOLD
                server.received.set()
                if reply is server.HOLD:
                    server.held.set()
                    if not server._hold(self.connection):
                        return
                    reply = server._held_reply
                body = json.dumps(reply).encode()
                try:
                    self.send_response(server.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    server.disconnected.set()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def release(self, reply: dict) -> None:
        self._held_reply = reply
        self._release.set()

    def _hold(self, connection) -> bool:
        """Wait for release; returns False once the client has closed its side of the connection."""
        while not self._release.is_set():
            readable, _, _ = select.select([connection], [], [], 0.01)
            if readable:
                try:
                    if connection.recv(1, socket.MSG_PEEK) == b"":
                        self.disconnected.set()
                        return False
                except OSError:
                    self.disconnected.set()
                    return False
        return True

    def close(self) -> None:
        self._release.set()
        self._server.shutdown()
        self._server.server_close()


def chat_reply(content: str | None = None, tool_calls: list | None = None) -> dict:
    message: dict = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}
