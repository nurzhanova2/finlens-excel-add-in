"""Sandbox runner port (AD-4): the only component that executes generated Python.

Every job is a fresh `docker run` container: non-root, no network, read-only root filesystem, no host
mounts, no environment secrets, all capabilities dropped, no-new-privileges, pids/memory/CPU/wall-time
limits and a small noexec tmpfs. Input goes in through stdin and the typed result comes out through
stdout, so nothing is written to the host disk. If no container runtime is available the job fails
with SANDBOX_UNAVAILABLE: generated code is never executed on the host.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import ValidationError

from app.domain.computation import MAX_CHARTS, MAX_PNG_BYTES, RunnerFailure, RunnerSuccess, SafeDiagnostic
from app.domain.contracts import TabularArtifact

logger = logging.getLogger(__name__)

DEFAULT_IMAGE = "finlens-sandbox:1"
JOB_LABEL = "finlens.sandbox-job"
# Protocol output: PNGs (base64) + tables + stdout, with headroom; anything larger is an output-limit failure.
MAX_OUTPUT_BYTES = MAX_CHARTS * (MAX_PNG_BYTES * 4 // 3 + 1_000) + 1_500_000
MAX_STDERR_BYTES = 16_000
_IMAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,200}(:[A-Za-z0-9._-]{1,128})?(@sha256:[a-f0-9]{64})?$")
_RUNTIME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# Only what the docker CLI needs to find the daemon; FINLENS_* secrets never reach the CLI or the job.
# How often a running job checks for cancellation and its wall-time deadline.
_CANCEL_POLL_SECONDS = 0.1
_CLI_ENV = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY", "SYSTEMROOT", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "TEMP", "TMP")


class SandboxUnavailable(Exception):
    """No usable container runtime or image: the job is not run anywhere."""


class SandboxBusy(Exception):
    pass


class SandboxCancelled(Exception):
    """The analysis was cancelled: the container was force-removed and its output discarded."""


@dataclass(frozen=True)
class SandboxLimits:
    wall_seconds: int = 30
    cpu_seconds: int = 30
    memory_mb: int = 512
    cpus: float = 1.0
    pids: int = 64
    tmpfs_mb: int = 32
    nofile: int = 128
    # Container start-up and library import time on top of the analysis wall time.
    startup_grace_seconds: int = 20


@dataclass(frozen=True)
class SandboxInput:
    ref: str
    artifact: TabularArtifact


@dataclass(frozen=True)
class SandboxJob:
    analysis_id: str
    attempt: int
    code: str
    inputs: tuple[SandboxInput, ...]
    # Set by the analysis lifecycle on cancellation (Story 3.1); the runner then terminates the job.
    cancel: threading.Event | None = field(default=None, compare=False, repr=False)

    @property
    def cancelled(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()


@dataclass(frozen=True)
class ExecutionSucceeded:
    output: RunnerSuccess
    duration_ms: int


@dataclass(frozen=True)
class ExecutionFailed:
    diagnostic: SafeDiagnostic
    duration_ms: int


ExecutionOutcome = ExecutionSucceeded | ExecutionFailed


class SandboxRunner(Protocol):
    def run(self, job: SandboxJob) -> ExecutionOutcome: ...


def job_payload(job: SandboxJob, limits: SandboxLimits) -> bytes:
    """Copies of the referenced artifacts only; no session, workbook map or other request data."""
    return json.dumps({
        "code": job.code,
        "inputs": [
            {"ref": item.ref, "sheetName": item.artifact.sheetName, "address": item.artifact.address, "values": item.artifact.values, "formulas": item.artifact.formulas, "numberFormats": item.artifact.numberFormats}
            for item in job.inputs
        ],
        "limits": {"wallSeconds": limits.wall_seconds, "cpuSeconds": limits.cpu_seconds},
    }, ensure_ascii=False, default=str).encode("utf-8")


def build_docker_command(docker: str, image: str, name: str, limits: SandboxLimits, oci_runtime: str | None = None, entrypoint: list[str] | None = None) -> list[str]:
    command = [
        docker, "run", "--rm", "--interactive",
        "--name", name,
        "--label", f"{JOB_LABEL}=1",
        "--pull", "never",
        "--network", "none",
        "--read-only",
        "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_mb}m,mode=1777",
        "--user", "10001:10001",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges=true",
        "--ipc", "none",
        "--pids-limit", str(limits.pids),
        "--memory", f"{limits.memory_mb}m",
        "--memory-swap", f"{limits.memory_mb}m",
        "--cpus", f"{limits.cpus:g}",
        "--ulimit", f"nofile={limits.nofile}:{limits.nofile}",
        "--ulimit", "core=0:0",
        "--log-driver", "none",
        "--hostname", "sandbox",
        "--workdir", "/tmp",
        "--stop-timeout", "1",
    ]
    if oci_runtime:
        command += ["--runtime", oci_runtime]
    if entrypoint:
        command += ["--entrypoint", entrypoint[0], image, *entrypoint[1:]]
    else:
        command.append(image)
    return command


def _failure(code: str, error_type: str, message: str, duration_ms: int) -> ExecutionFailed:
    return ExecutionFailed(SafeDiagnostic(code=code, errorType=error_type, message=message), duration_ms)


def _feed(stream, payload: bytes) -> None:
    try:
        stream.write(payload)
        stream.close()
    except (BrokenPipeError, OSError, ValueError):
        pass


class _BoundedReader(threading.Thread):
    def __init__(self, stream, limit: int):
        super().__init__(daemon=True)
        self._stream, self._limit = stream, limit
        self.data = bytearray()
        self.overflow = False

    def run(self) -> None:
        while chunk := self._stream.read(65_536):
            if len(self.data) + len(chunk) > self._limit:
                self.overflow = True
                continue  # keep draining so the job never blocks on a full pipe
            self.data.extend(chunk)


@dataclass
class DockerSandboxRunner:
    image: str = DEFAULT_IMAGE
    limits: SandboxLimits = field(default_factory=SandboxLimits)
    docker: str | None = None
    oci_runtime: str | None = None
    max_concurrent: int = 2
    queue_seconds: int = 60

    def __post_init__(self) -> None:
        if not _IMAGE_PATTERN.match(self.image):
            raise ValueError("invalid sandbox image reference")
        if self.oci_runtime and not _RUNTIME_PATTERN.match(self.oci_runtime):
            raise ValueError("invalid OCI runtime name")
        self._slots = threading.BoundedSemaphore(self.max_concurrent)
        self._checked_at = 0.0

    def _cli(self) -> str:
        docker = self.docker or shutil.which("docker")
        if not docker:
            raise SandboxUnavailable("docker CLI not found")
        return docker

    @staticmethod
    def _env() -> dict[str, str]:
        return {name: os.environ[name] for name in _CLI_ENV if name in os.environ}

    def ensure_available(self) -> None:
        if time.monotonic() - self._checked_at < 30:
            return
        try:
            probe = subprocess.run([self._cli(), "image", "inspect", "--format", "{{.Id}}", self.image], capture_output=True, timeout=15, env=self._env(), check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SandboxUnavailable("container runtime did not respond") from error
        if probe.returncode != 0:
            raise SandboxUnavailable("container runtime or sandbox image unavailable")
        self._checked_at = time.monotonic()

    def run(self, job: SandboxJob) -> ExecutionOutcome:
        self.ensure_available()
        deadline = time.monotonic() + self.queue_seconds
        while not self._slots.acquire(timeout=_CANCEL_POLL_SECONDS):
            if job.cancelled:
                raise SandboxCancelled()
            if time.monotonic() >= deadline:
                raise SandboxBusy()
        try:
            return self._run(job)
        finally:
            self._slots.release()

    def _run(self, job: SandboxJob) -> ExecutionOutcome:
        if job.cancelled:
            raise SandboxCancelled()
        docker = self._cli()
        name = f"finlens-job-{secrets.token_hex(12)}"
        command = build_docker_command(docker, self.image, name, self.limits, self.oci_runtime)
        started = time.monotonic()
        timed_out = cancelled = False
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._env())
        except OSError as error:
            raise SandboxUnavailable("container runtime could not be started") from error
        stdout, stderr = _BoundedReader(process.stdout, MAX_OUTPUT_BYTES), _BoundedReader(process.stderr, MAX_STDERR_BYTES)
        stdout.start()
        stderr.start()
        writer = threading.Thread(target=_feed, args=(process.stdin, job_payload(job, self.limits)), daemon=True)
        writer.start()
        try:
            deadline = started + self.limits.wall_seconds + self.limits.startup_grace_seconds
            while True:
                try:
                    process.wait(timeout=_CANCEL_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    pass
                cancelled = job.cancelled
                timed_out = time.monotonic() >= deadline
                if cancelled or timed_out:
                    # Same teardown for cancellation and timeout: the container is removed, then the CLI is killed.
                    self._remove(docker, name)
                    process.kill()
                    process.wait(timeout=10)
                    break
            stdout.join(timeout=5)
            stderr.join(timeout=5)
        finally:
            # --rm already removes the container; this also covers timeouts, kills and API crashes mid-job.
            self._remove(docker, name)
            for stream in (process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
        duration_ms = round((time.monotonic() - started) * 1000)
        if cancelled or job.cancelled:
            # The output of a cancelled job is never parsed or returned.
            stdout.data.clear()
            stderr.data.clear()
            logger.info("sandbox_job analysisId=%s attempt=%s outcome=cancelled exitCode=%s durationMs=%s", job.analysis_id, job.attempt, process.returncode, duration_ms)
            raise SandboxCancelled()
        outcome = self._classify(process.returncode, bytes(stdout.data), stdout.overflow, bytes(stderr.data), timed_out, duration_ms)
        logger.info("sandbox_job analysisId=%s attempt=%s outcome=%s exitCode=%s durationMs=%s outputBytes=%s", job.analysis_id, job.attempt, "succeeded" if isinstance(outcome, ExecutionSucceeded) else outcome.diagnostic.code, process.returncode, duration_ms, len(stdout.data))
        return outcome

    def _remove(self, docker: str, name: str) -> None:
        try:
            subprocess.run([docker, "rm", "--force", "--volumes", name], capture_output=True, timeout=15, env=self._env(), check=False)
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("sandbox_cleanup_failed")

    def _classify(self, exit_code: int | None, output: bytes, overflow: bool, stderr: bytes, timed_out: bool, duration_ms: int) -> ExecutionOutcome:
        if timed_out:
            return _failure("TIMEOUT", "TimeoutError", f"the job exceeded its wall-time limit of {self.limits.wall_seconds} s and was terminated", duration_ms)
        if overflow:
            return _failure("OUTPUT_LIMIT", "OutputLimitError", "the job output exceeded the allowed size", duration_ms)
        if exit_code in (125, 126, 127) and not output:
            # docker could not create or start the container (daemon down, image missing, bad runtime).
            raise SandboxUnavailable("container could not be started")
        if exit_code == 137 and not output and duration_ms >= self.limits.cpu_seconds * 1000:
            # SIGKILL from the hard CPU rlimit after the code swallowed the soft-limit signal.
            return _failure("CPU_LIMIT", "CpuLimitExceeded", f"the job was terminated after exceeding its CPU-time limit of {self.limits.cpu_seconds} s", duration_ms)
        if exit_code == 137 and not output:
            return _failure("MEMORY_LIMIT", "MemoryError", f"the job was terminated, most likely after exceeding the {self.limits.memory_mb} MB memory limit", duration_ms)
        if not output:
            return _failure("SANDBOX_CRASHED", "SandboxError", "the job ended without a result", duration_ms)
        try:
            raw = json.loads(output)
        except ValueError:
            return _failure("INVALID_OUTPUT", "OutputError", "the job returned an unreadable result", duration_ms)
        status = raw.get("status") if isinstance(raw, dict) else None
        try:
            if status == "succeeded":
                return ExecutionSucceeded(RunnerSuccess.model_validate(raw), duration_ms)
            if status == "failed":
                return ExecutionFailed(RunnerFailure.model_validate(raw).error, duration_ms)
        except ValidationError:
            pass
        return _failure("INVALID_OUTPUT", "OutputError", "the job result violated the output limits or schema", duration_ms)


class UnavailableSandboxRunner:
    """Used when sandboxing is disabled or misconfigured: an honest failure, never host execution."""

    def run(self, job: SandboxJob) -> ExecutionOutcome:
        raise SandboxUnavailable("sandbox disabled")


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, default))
    except ValueError:
        return default
    return value if low <= value <= high else default


def build_sandbox_runner() -> SandboxRunner:
    if os.getenv("FINLENS_SANDBOX_RUNTIME", "docker") != "docker":
        return UnavailableSandboxRunner()
    limits = SandboxLimits(
        wall_seconds=_int_env("FINLENS_SANDBOX_TIMEOUT_SECONDS", 30, 5, 120),
        cpu_seconds=_int_env("FINLENS_SANDBOX_TIMEOUT_SECONDS", 30, 5, 120),
        memory_mb=_int_env("FINLENS_SANDBOX_MEMORY_MB", 512, 128, 2048),
    )
    try:
        return DockerSandboxRunner(
            image=os.getenv("FINLENS_SANDBOX_IMAGE", DEFAULT_IMAGE),
            limits=limits,
            oci_runtime=os.getenv("FINLENS_SANDBOX_OCI_RUNTIME") or None,
            max_concurrent=_int_env("FINLENS_SANDBOX_MAX_CONCURRENT", 2, 1, 16),
        )
    except ValueError:
        logger.warning("sandbox_config_invalid")
        return UnavailableSandboxRunner()
