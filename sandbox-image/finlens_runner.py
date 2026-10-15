"""FinLens sandbox runner.

Runs exactly one validated `run_analysis` job inside an ephemeral, network-less, read-only container:
stdin carries one JSON job (code + copied tabular artifacts), stdout carries one JSON result.
The container is the security boundary; the restrictions below are defense in depth and make
failures explicit and safe to describe to the model.
"""
from __future__ import annotations

import base64
import builtins
import io
import json
import math
import os
import re
import resource
import shutil
import signal
import sys
import time

MAX_INPUT_BYTES = 8_000_000
MAX_CODE_CHARS = 20_000
MAX_WALL_SECONDS = 60
MAX_CPU_SECONDS = 60
MAX_TABLES = 5
MAX_TABLE_ROWS = 200
MAX_TABLE_COLUMNS = 30
MAX_CELL_CHARS = 200
MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 300
MAX_CHARTS = 3
MAX_PNG_BYTES = 1_000_000
MAX_PNG_SIDE = 2_000
CHART_DPI = 100
MAX_STDOUT_CHARS = 4_000
MAX_MESSAGE_CHARS = 400
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_OPEN_FILES = 128

CODE_FILENAME = "<analysis>"
FONT_CACHE_SOURCE = "/opt/finlens/mpl"
MPL_CONFIG_DIR = "/tmp/matplotlib"

# Fully qualified modules generated code may import; everything else fails with IMPORT_NOT_ALLOWED.
ALLOWED_IMPORTS = frozenset({
    "pandas", "numpy", "numpy.linalg", "numpy.random", "matplotlib", "matplotlib.pyplot", "matplotlib.ticker",
    "matplotlib.dates", "matplotlib.colors", "math", "statistics", "datetime", "decimal", "fractions",
    "itertools", "functools", "collections", "re", "json", "string", "typing", "dataclasses", "operator",
    "calendar", "textwrap", "copy", "numbers", "enum", "heapq", "bisect",
})
REMOVED_BUILTINS = (
    "open", "exec", "eval", "compile", "globals", "locals", "vars", "getattr", "setattr", "delattr", "input",
    "breakpoint", "help", "exit", "quit", "copyright", "credits", "license", "memoryview", "__loader__", "__spec__",
)
_PATH = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][\w.@+-]+){2,}[\\/]?")
_FILE_FRAGMENT = re.compile(r'File "[^"]*"(?:, line \d+)?(?:, in \S+)?')


class JobError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ImportBlocked(ImportError):
    pass


class OutputLimitError(Exception):
    pass


class WallTimeout(Exception):
    pass


class CpuLimit(Exception):
    pass


def sanitize(text: str) -> str:
    text = _FILE_FRAGMENT.sub("", str(text))
    text = _PATH.sub("<path>", text)
    text = " ".join(text.split())
    return text[:MAX_MESSAGE_CHARS]


def column_letters(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def top_left(address: str) -> tuple[int, int]:
    local = address.rsplit("!", 1)[-1].split(":")[0].replace("$", "").upper()
    match = re.fullmatch(r"([A-Z]{1,3})([0-9]{1,7})", local)
    if not match:
        return 1, 1
    column = 0
    for letter in match.group(1):
        column = column * 26 + ord(letter) - 64
    return int(match.group(2)), column


def read_job(stream) -> dict:
    raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise JobError("INVALID_JOB", "job exceeds the input size limit")
    try:
        job = json.loads(raw)
    except ValueError as error:
        raise JobError("INVALID_JOB", "job is not valid JSON") from error
    if not isinstance(job, dict) or not isinstance(job.get("code"), str) or not isinstance(job.get("inputs"), list):
        raise JobError("INVALID_JOB", "job has an invalid shape")
    if len(job["code"]) > MAX_CODE_CHARS:
        raise JobError("INVALID_JOB", "code exceeds the size limit")
    return job


class BoundedWriter(io.TextIOBase):
    """Captures print() output of the analysis, keeping at most MAX_STDOUT_CHARS."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.size = 0
        self.truncated = False

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        remaining = MAX_STDOUT_CHARS - self.size
        if remaining <= 0:
            self.truncated = self.truncated or bool(text)
            return len(text)
        piece = text[:remaining]
        self.parts.append(piece)
        self.size += len(piece)
        self.truncated = self.truncated or len(piece) < len(text)
        return len(text)

    def value(self) -> str:
        return "".join(self.parts)


def prepare_matplotlib():
    os.environ["MPLCONFIGDIR"] = MPL_CONFIG_DIR
    if os.path.isdir(FONT_CACHE_SOURCE) and not os.path.isdir(MPL_CONFIG_DIR):
        shutil.copytree(FONT_CACHE_SOURCE, MPL_CONFIG_DIR)
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


class Analysis:
    """Helpers exposed to generated code; inputs are copies, outputs are bounded and JSON-safe."""

    def __init__(self, inputs: list, pd, np, plt):
        self.pd, self.np, self.plt = pd, np, plt
        self.inputs: dict[str, dict] = {}
        for item in inputs:
            if not isinstance(item, dict) or not isinstance(item.get("ref"), str) or not isinstance(item.get("values"), list):
                raise JobError("INVALID_JOB", "input artifact has an invalid shape")
            self.inputs[item["ref"]] = item
        self.tables: list[dict] = []
        self.charts: list[dict] = []

    def _input(self, ref) -> dict:
        if ref not in self.inputs:
            raise KeyError(f"unknown artifact {ref!r}; available: {', '.join(sorted(self.inputs))}")
        return self.inputs[ref]

    def frame(self, ref):
        """Raw cells indexed by Excel row numbers with Excel column letters as column labels."""
        item = self._input(ref)
        top, left = top_left(item.get("address", "A1"))
        rows = [[None if cell == "" else cell for cell in row] for row in item["values"]]
        width = len(rows[0]) if rows else 0
        return self.pd.DataFrame(rows, index=range(top, top + len(rows)), columns=[column_letters(left + i) for i in range(width)])

    def table(self, ref, header_row: int = 1):
        """Uses one row of the artifact (1-based, relative to the artifact) as column headers."""
        raw = self.frame(ref)
        if not 1 <= header_row <= len(raw):
            raise ValueError(f"header_row must be between 1 and {len(raw)}")
        header = raw.iloc[header_row - 1]
        names, seen = [], set()
        for letter, value in header.items():
            name = str(value).strip() if value is not None else ""
            name = name or letter
            while name in seen:
                name = f"{name} ({letter})"
            seen.add(name)
            names.append(name)
        body = raw.iloc[header_row:].copy()
        body.columns = names
        return body

    def artifact_info(self, ref) -> dict:
        item = self._input(ref)
        values = item["values"]
        return {
            "sheetName": item.get("sheetName"),
            "address": item.get("address"),
            "rows": len(values),
            "columns": len(values[0]) if values else 0,
            "formulas": [list(row) for row in item.get("formulas", [])],
            "numberFormats": [list(row) for row in item.get("numberFormats", [])],
        }

    def _cell(self, value):
        np, pd = self.np, self.pd
        if value is None:
            return None
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value if abs(value) < 2**53 else float(value)
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if value is pd.NaT:
            return None
        if hasattr(value, "isoformat"):
            return value.isoformat()[:MAX_CELL_CHARS]
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        return str(value)[:MAX_CELL_CHARS]

    def show_table(self, data, title: str) -> None:
        pd = self.pd
        if len(self.tables) >= MAX_TABLES:
            raise OutputLimitError(f"at most {MAX_TABLES} tables can be shown")
        title = _text(title, "title", MAX_TITLE_CHARS)
        if isinstance(data, pd.Series):
            data = data.to_frame()
        if not isinstance(data, pd.DataFrame):
            raise TypeError("show_table expects a pandas DataFrame or Series")
        if not isinstance(data.index, pd.RangeIndex) or data.index.start != 0 or data.index.step != 1:
            data = data.reset_index()
        if data.shape[1] > MAX_TABLE_COLUMNS:
            raise OutputLimitError(f"a table can have at most {MAX_TABLE_COLUMNS} columns")
        shown = data.head(MAX_TABLE_ROWS)
        self.tables.append({
            "title": title,
            "columns": [str(column)[:MAX_CELL_CHARS] for column in data.columns],
            "rows": [[self._cell(value) for value in row] for row in shown.itertuples(index=False, name=None)],
            "totalRows": int(data.shape[0]),
            "truncated": bool(data.shape[0] > MAX_TABLE_ROWS),
        })

    def show_chart(self, fig, title: str, metric: str, period: str, insight: str) -> None:
        from matplotlib.figure import Figure

        if len(self.charts) >= MAX_CHARTS:
            raise OutputLimitError(f"at most {MAX_CHARTS} charts can be shown")
        if not isinstance(fig, Figure):
            raise TypeError("show_chart expects a matplotlib Figure")
        description = {
            "metric": _text(metric, "metric", MAX_DESCRIPTION_CHARS),
            "period": _text(period, "period", MAX_DESCRIPTION_CHARS),
            "insight": _text(insight, "insight", MAX_DESCRIPTION_CHARS),
        }
        width, height = fig.get_size_inches()
        scale = min(1.0, MAX_PNG_SIDE / (width * CHART_DPI), MAX_PNG_SIDE / (height * CHART_DPI))
        if scale < 1.0:
            fig.set_size_inches(width * scale, height * scale)
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=CHART_DPI)
        png = buffer.getvalue()
        self.plt.close(fig)
        if len(png) > MAX_PNG_BYTES:
            raise OutputLimitError(f"a chart PNG can be at most {MAX_PNG_BYTES} bytes")
        width_px, height_px = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
        self.charts.append({"title": _text(title, "title", MAX_TITLE_CHARS), "pngBase64": base64.b64encode(png).decode("ascii"), "widthPx": width_px, "heightPx": height_px, "description": description})


def _text(value, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()[:limit]


def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level != 0:
        raise ImportBlocked("relative imports are not allowed")
    if name not in ALLOWED_IMPORTS:
        raise ImportBlocked(f"import of {name!r} is not allowed; use pandas, numpy, matplotlib or the safe standard modules")
    for item in fromlist or ():
        qualified = f"{name}.{item}"
        if qualified not in ALLOWED_IMPORTS and _is_module(name, item):
            raise ImportBlocked(f"import of {qualified!r} is not allowed")
    return builtins.__import__(name, globals, locals, fromlist, level)


def _is_module(package: str, item: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(f"{package}.{item}") is not None
    except (ImportError, ValueError):
        return False


def safe_builtins() -> dict:
    allowed = dict(vars(builtins))
    for name in REMOVED_BUILTINS:
        allowed.pop(name, None)
    allowed["__import__"] = guarded_import
    return allowed


def _error_line(traceback_object) -> int | None:
    line = None
    while traceback_object is not None:
        if traceback_object.tb_frame.f_code.co_filename == CODE_FILENAME:
            line = traceback_object.tb_lineno
        traceback_object = traceback_object.tb_next
    return line


def failure(code: str, error_type: str, message: str, line: int | None = None) -> dict:
    return {"status": "failed", "error": {"code": code, "errorType": error_type[:64], "message": sanitize(message), "line": line}}


def _raise_timeout(*_):
    raise WallTimeout("the analysis exceeded its wall-time limit")


def _raise_cpu(*_):
    raise CpuLimit("the analysis exceeded its CPU-time limit")


def apply_limits(cpu_seconds: int) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (MAX_OPEN_FILES, MAX_OPEN_FILES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.signal(signal.SIGXCPU, _raise_cpu)


def execute(job: dict) -> dict:
    limits = job.get("limits") if isinstance(job.get("limits"), dict) else {}
    wall = max(1, min(int(limits.get("wallSeconds", 30)), MAX_WALL_SECONDS))
    cpu = max(1, min(int(limits.get("cpuSeconds", 30)), MAX_CPU_SECONDS))

    plt = prepare_matplotlib()
    import numpy as np
    import pandas as pd

    analysis = Analysis(job["inputs"], pd, np, plt)
    try:
        compiled = compile(job["code"], CODE_FILENAME, "exec", dont_inherit=True)
    except SyntaxError as error:
        return failure("SYNTAX_ERROR", "SyntaxError", error.msg or "invalid syntax", error.lineno)

    namespace = {
        "__builtins__": safe_builtins(),
        "__name__": "__analysis__",
        "pd": pd,
        "np": np,
        "plt": plt,
        "frame": analysis.frame,
        "table": analysis.table,
        "artifact_info": analysis.artifact_info,
        "show_table": analysis.show_table,
        "show_chart": analysis.show_chart,
    }
    captured = BoundedWriter()
    real_stdout = sys.stdout
    started = time.monotonic()
    apply_limits(cpu)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.alarm(wall)
    try:
        sys.stdout = captured
        exec(compiled, namespace)  # noqa: S102 - generated code is the job; the container is the boundary
    except WallTimeout as error:
        return failure("TIMEOUT", "TimeoutError", str(error), _error_line(error.__traceback__))
    except CpuLimit as error:
        return failure("CPU_LIMIT", "CpuLimitExceeded", str(error), _error_line(error.__traceback__))
    except MemoryError as error:
        return failure("MEMORY_LIMIT", "MemoryError", "the analysis exceeded its memory limit", _error_line(error.__traceback__))
    except ImportBlocked as error:
        return failure("IMPORT_NOT_ALLOWED", "ImportError", str(error), _error_line(error.__traceback__))
    except OutputLimitError as error:
        return failure("OUTPUT_LIMIT", "OutputLimitError", str(error), _error_line(error.__traceback__))
    except BaseException as error:  # noqa: BLE001 - every failure becomes a bounded diagnostic
        return failure("RUNTIME_ERROR", type(error).__name__, str(error), _error_line(error.__traceback__))
    finally:
        signal.alarm(0)
        sys.stdout = real_stdout
        plt.close("all")
    if not analysis.tables and not analysis.charts and not captured.value().strip():
        return failure("NO_OUTPUT", "NoOutput", "the code produced no table, chart or printed result; call show_table, show_chart or print")
    return {
        "status": "succeeded",
        "tables": analysis.tables,
        "charts": analysis.charts,
        "stdout": captured.value(),
        "stdoutTruncated": captured.truncated,
        "durationMs": round((time.monotonic() - started) * 1000),
    }


def main() -> int:
    # The protocol channel is a private duplicate of stdout; fd 1/2 are silenced for the analysis.
    protocol = os.fdopen(os.dup(1), "w", encoding="utf-8")
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    try:
        result = execute(read_job(sys.stdin.buffer))
    except JobError as error:
        result = failure(error.code, "JobError", str(error))
    except BaseException as error:  # noqa: BLE001
        result = failure("SANDBOX_ERROR", type(error).__name__, "the sandbox runner failed")
    try:
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        encoded = json.dumps(failure("INVALID_OUTPUT", "OutputError", "the analysis produced values that cannot be returned"))
    protocol.write(encoded)
    protocol.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
