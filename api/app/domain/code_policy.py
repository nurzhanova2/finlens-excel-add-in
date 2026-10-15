"""Static policy for generated analysis code (AD-3, AD-4).

This check runs on the API before any sandbox job is created. It is defense in depth: the hardened
container is the real boundary, but obviously unsafe code (subprocess, network, files, secrets,
Excel/Office, dynamic code, non-allowlisted imports) never reaches it and the model gets a clear reason.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass

# Mirrors ALLOWED_IMPORTS in sandbox-image/finlens_runner.py.
ALLOWED_IMPORTS = frozenset({
    "pandas", "numpy", "numpy.linalg", "numpy.random", "matplotlib", "matplotlib.pyplot", "matplotlib.ticker",
    "matplotlib.dates", "matplotlib.colors", "math", "statistics", "datetime", "decimal", "fractions",
    "itertools", "functools", "collections", "re", "json", "string", "typing", "dataclasses", "operator",
    "calendar", "textwrap", "copy", "numbers", "enum", "heapq", "bisect",
})

FORBIDDEN_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "input", "breakpoint", "help", "exit", "quit", "memoryview", "__builtins__", "__loader__", "__spec__",
    "os", "sys", "subprocess", "socket", "shutil", "pathlib", "importlib", "ctypes", "builtins", "urllib",
    "requests", "http", "Office", "Excel",
})

# Attribute names that reach the OS, files, the network or interpreter internals through allowed libraries.
FORBIDDEN_ATTRIBUTES = frozenset({
    "os", "sys", "subprocess", "system", "popen", "spawn", "fork", "execv", "execve", "socket", "shutil", "pathlib",
    "importlib", "ctypes", "ctypeslib", "builtins", "io", "common", "modules", "savefig", "tofile", "fromfile",
    "load", "save", "savez", "savez_compressed", "loadtxt", "savetxt", "genfromtxt", "fromregex", "memmap",
    "DataSource", "imread", "imsave", "urlopen", "request", "environ", "getenv", "putenv", "f_globals", "f_locals",
    "f_back", "gi_frame", "cr_frame", "tb_frame", "Office", "Excel",
})
_PANDAS_IO = re.compile(r"^(read_\w+|to_(csv|excel|pickle|parquet|feather|hdf|sql|json|html|xml|stata|latex|clipboard|orc|gbq|markdown))$")

_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,15}://|\bwww\.[a-z0-9-]+\.[a-z]")
_PATH = re.compile(r"^(?:~[\\/]|\.{1,2}[\\/]|/(?:[\w.-]+)|[A-Za-z]:[\\/]|\\\\)")
_SECRET = re.compile(r"(?i)(api[_-]?key|secret|passw(or)?d|bearer\s|authorization|private[_-]?key|FINLENS_|QWEN_)")


@dataclass(frozen=True)
class PolicyViolation:
    """Safe, bounded reason that can be returned to the model."""

    reason: str
    line: int | None = None


def check_text(value: str, field: str) -> PolicyViolation | None:
    """Reject free-text tool fields that carry URLs, file paths or credentials."""
    if _URL.search(value):
        return PolicyViolation(f"{field} must not contain URLs")
    if _SECRET.search(value):
        return PolicyViolation(f"{field} must not reference credentials or secrets")
    return None


def _string_violation(value: str) -> str | None:
    if _URL.search(value):
        return "string literals must not contain URLs"
    if _PATH.match(value.strip()):
        return "string literals must not contain file paths"
    if _SECRET.search(value):
        return "string literals must not reference credentials or secrets"
    return None


def check_code(code: str) -> PolicyViolation | None:
    try:
        tree = ast.parse(code, filename="<analysis>", mode="exec")
    except SyntaxError as error:
        return PolicyViolation(f"syntax error: {(error.msg or 'invalid syntax')[:120]}", error.lineno)
    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_IMPORTS:
                    return PolicyViolation(f"import of '{alias.name[:60]}' is not allowed; only pandas, numpy, matplotlib and safe standard modules are available", line)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level or module not in ALLOWED_IMPORTS:
                return PolicyViolation(f"import from '{module[:60]}' is not allowed; only pandas, numpy, matplotlib and safe standard modules are available", line)
            for alias in node.names:
                if alias.name == "*" or f"{module}.{alias.name}" in _SUBMODULE_DENYLIST:
                    return PolicyViolation(f"import of '{module}.{alias.name[:40]}' is not allowed", line)
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            return PolicyViolation(f"use of '{node.id}' is not allowed in the sandbox", line)
        elif isinstance(node, ast.Name) and node.id.startswith("__"):
            return PolicyViolation("dunder names are not allowed", line)
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                return PolicyViolation("private and dunder attributes are not allowed", line)
            if node.attr in FORBIDDEN_ATTRIBUTES or _PANDAS_IO.match(node.attr):
                return PolicyViolation(f"attribute '{node.attr}' (files, OS, network or interpreter access) is not allowed", line)
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            text = node.value if isinstance(node.value, str) else node.value.decode("latin-1")
            reason = _string_violation(text)
            if reason:
                return PolicyViolation(reason, line)
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith, ast.Global, ast.Nonlocal)):
            return PolicyViolation("async code, global and nonlocal are not allowed", line)
    return None


# Submodules reachable through `from <allowed> import <name>` that expose files, processes or C bindings.
_SUBMODULE_DENYLIST = frozenset({
    "numpy.ctypeslib", "numpy.distutils", "numpy.f2py", "numpy.testing", "pandas.io", "pandas.testing",
    "matplotlib.font_manager", "matplotlib.image", "matplotlib.backends", "matplotlib.testing",
})
