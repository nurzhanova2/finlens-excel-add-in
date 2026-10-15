"""Story 2.1: hardening flags, honest unavailability, limits and cleanup of the Docker sandbox adapter.

These tests replace the docker CLI with a small script, so they need no container runtime and never
execute analysis code on the host.
"""
import json
import os
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.adapters.sandbox import (
    DockerSandboxRunner,
    ExecutionFailed,
    ExecutionSucceeded,
    SandboxCancelled,
    SandboxInput,
    SandboxJob,
    SandboxLimits,
    SandboxUnavailable,
    UnavailableSandboxRunner,
    build_docker_command,
    build_sandbox_runner,
    job_payload,
)
from app.domain.contracts import TabularArtifact
from tests.support import SECRET_CELL, artifact, runner_success

ARTIFACT = TabularArtifact.model_validate(artifact("P&L", "P&L!A1:B2", 2, 2, SECRET_CELL))
JOB = SandboxJob(analysis_id="analysis_0001", attempt=1, code="print(1)", inputs=(SandboxInput("artifact_1", ARTIFACT),))


def fake_docker(directory: Path, run_body: str) -> tuple[str, Path]:
    """Create an executable stand-in for the docker CLI that logs every invocation."""
    log = directory / "calls.jsonl"
    script = directory / "docker"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys, time
        with open({str(log)!r}, "a") as handle:
            handle.write(json.dumps({{"argv": sys.argv[1:], "env": sorted(os.environ)}}) + "\\n")
        if sys.argv[1:3] == ["image", "inspect"]:
            print("sha256:abc"); sys.exit(0)
        if sys.argv[1] == "rm":
            sys.exit(0)
        stdin = sys.stdin.buffer.read()
        """) + textwrap.dedent(run_body))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), log


def calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


class DockerCommandTests(unittest.TestCase):
    def test_every_job_is_hardened(self):
        command = build_docker_command("docker", "finlens-sandbox:1", "finlens-job-x", SandboxLimits())
        text = " ".join(command)
        for flag in ("--rm", "--network none", "--read-only", "--user 10001:10001", "--cap-drop ALL", "--security-opt no-new-privileges=true",
                     "--pids-limit 64", "--memory 512m", "--memory-swap 512m", "--cpus 1", "--pull never", "--log-driver none", "--ipc none",
                     "--tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m"):
            self.assertIn(flag, text)
        for forbidden in ("-v", "--volume", "--mount", "-e", "--env", "--env-file", "--privileged", "--network host", "-p", "--publish", "--cap-add", "unconfined", "docker.sock"):
            self.assertNotIn(forbidden, command)
            self.assertNotIn(f" {forbidden} ", f" {text} ")

    def test_payload_carries_only_referenced_artifact_copies(self):
        other = TabularArtifact.model_validate(artifact("Secret", "Secret!A1", 1, 1, "OTHER"))
        payload = json.loads(job_payload(JOB, SandboxLimits()))
        self.assertEqual([item["ref"] for item in payload["inputs"]], ["artifact_1"])
        self.assertNotIn("OTHER", json.dumps(payload))
        self.assertEqual(set(payload), {"code", "inputs", "limits"})
        del other

    def test_configuration_is_validated(self):
        with self.assertRaises(ValueError):
            DockerSandboxRunner(image="--privileged")
        with self.assertRaises(ValueError):
            DockerSandboxRunner(oci_runtime="runc; rm -rf /")
        with patch.dict(os.environ, {"FINLENS_SANDBOX_RUNTIME": "disabled"}, clear=True):
            self.assertIsInstance(build_sandbox_runner(), UnavailableSandboxRunner)
        with patch.dict(os.environ, {"FINLENS_SANDBOX_IMAGE": "bad image"}, clear=True):
            self.assertIsInstance(build_sandbox_runner(), UnavailableSandboxRunner)
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(build_sandbox_runner(), DockerSandboxRunner)


class DockerRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())

    def runner(self, body: str, **limits) -> tuple[DockerSandboxRunner, Path]:
        docker, log = fake_docker(self.directory, body)
        return DockerSandboxRunner(docker=docker, limits=SandboxLimits(**limits)), log

    def test_missing_runtime_is_unavailable_and_nothing_runs_on_host(self):
        with self.assertRaises(SandboxUnavailable):
            DockerSandboxRunner(docker=str(self.directory / "missing-docker")).run(JOB)
        with patch("app.adapters.sandbox.shutil.which", return_value=None), self.assertRaises(SandboxUnavailable):
            DockerSandboxRunner().run(JOB)
        with self.assertRaises(SandboxUnavailable):
            UnavailableSandboxRunner().run(JOB)

    def test_daemon_failure_is_unavailable(self):
        runner, _ = self.runner('sys.stderr.write("Cannot connect to the Docker daemon"); sys.exit(125)\n')
        with self.assertRaises(SandboxUnavailable):
            runner.run(JOB)

    def test_success_is_schema_validated_and_container_removed(self):
        output = runner_success().model_dump_json()
        runner, log = self.runner(f"assert b'artifact_1' in stdin\nsys.stdout.write({output!r})\n")
        with patch.dict(os.environ, {"FINLENS_QWEN_API_KEY": "server-secret", "PATH": os.environ["PATH"]}):
            outcome = runner.run(JOB)
        self.assertIsInstance(outcome, ExecutionSucceeded)
        recorded = calls(log)
        run = next(call for call in recorded if call["argv"][0] == "run")
        name = run["argv"][run["argv"].index("--name") + 1]
        self.assertTrue(any(call["argv"][:3] == ["rm", "--force", "--volumes"] and call["argv"][3] == name for call in recorded))
        # Server secrets never reach the docker CLI, let alone the container.
        self.assertTrue(all("FINLENS_QWEN_API_KEY" not in call["env"] for call in recorded))

    def test_wall_time_limit_kills_and_cleans_up(self):
        runner, log = self.runner("time.sleep(30)\n", wall_seconds=1, startup_grace_seconds=1)
        outcome = runner.run(JOB)
        self.assertIsInstance(outcome, ExecutionFailed)
        self.assertEqual(outcome.diagnostic.code, "TIMEOUT")
        self.assertGreaterEqual(sum(1 for call in calls(log) if call["argv"][0] == "rm"), 1)

    def test_output_size_limit(self):
        runner, _ = self.runner("sys.stdout.write('x' * 9_000_000)\n")
        outcome = runner.run(JOB)
        self.assertEqual(outcome.diagnostic.code, "OUTPUT_LIMIT")

    def test_out_of_memory_kill(self):
        runner, _ = self.runner("sys.exit(137)\n")
        self.assertEqual(runner.run(JOB).diagnostic.code, "MEMORY_LIMIT")

    def test_garbage_or_schema_violating_output_is_rejected(self):
        for body, code in (
            ("sys.stdout.write('not json')\n", "INVALID_OUTPUT"),
            ("sys.stdout.write(json.dumps({'status': 'succeeded', 'tables': [], 'charts': [{'title': 't', 'pngBase64': 'aGVsbG8=', 'widthPx': 1, 'heightPx': 1, 'description': {'metric': 'm', 'period': 'p', 'insight': 'i'}}], 'stdout': '', 'durationMs': 1}))\n", "INVALID_OUTPUT"),
            ("sys.stdout.write(json.dumps({'status': 'succeeded', 'tables': [{'title': 't', 'columns': ['a'], 'rows': [[1]] * 201, 'totalRows': 201, 'truncated': True}], 'charts': [], 'stdout': '', 'durationMs': 1}))\n", "INVALID_OUTPUT"),
            ("sys.exit(1)\n", "SANDBOX_CRASHED"),
        ):
            with self.subTest(code=code, body=body[:40]):
                runner, _ = self.runner(body)
                outcome = runner.run(JOB)
                self.assertIsInstance(outcome, ExecutionFailed)
                self.assertEqual(outcome.diagnostic.code, code)

    def test_runner_failure_diagnostic_is_passed_through_typed(self):
        failure = json.dumps({"status": "failed", "error": {"code": "RUNTIME_ERROR", "errorType": "KeyError", "message": "'Revenue'", "line": 3}})
        runner, _ = self.runner(f"sys.stdout.write({failure!r})\n")
        outcome = runner.run(JOB)
        self.assertEqual((outcome.diagnostic.code, outcome.diagnostic.errorType, outcome.diagnostic.line), ("RUNTIME_ERROR", "KeyError", 3))


class SandboxCancellationTests(unittest.TestCase):
    """Story 3.1: cancellation terminates the job; it never relaxes the hardening of the container."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())

    def test_cancel_force_removes_the_running_container_and_discards_output(self):
        output = runner_success().model_dump_json()
        docker, log = fake_docker(self.directory, f"time.sleep(30)\nsys.stdout.write({output!r})\n")
        runner = DockerSandboxRunner(docker=docker, limits=SandboxLimits(wall_seconds=30, startup_grace_seconds=5))
        cancel = threading.Event()
        job = SandboxJob(analysis_id="analysis_0001", attempt=2, code="print(1)", inputs=JOB.inputs, cancel=cancel)
        threading.Timer(0.5, cancel.set).start()
        started = time.monotonic()
        with self.assertRaises(SandboxCancelled):
            runner.run(job)
        self.assertLess(time.monotonic() - started, 10)
        recorded = calls(log)
        run = next(call for call in recorded if call["argv"][0] == "run")
        name = run["argv"][run["argv"].index("--name") + 1]
        self.assertTrue(any(call["argv"][:3] == ["rm", "--force", "--volumes"] and call["argv"][3] == name for call in recorded))

    def test_cancel_keeps_every_hardening_flag(self):
        docker, log = fake_docker(self.directory, "sys.stdout.write('not json')\n")
        runner = DockerSandboxRunner(docker=docker)
        runner.run(JOB)
        runner.run(SandboxJob(analysis_id="analysis_0001", attempt=1, code="print(1)", inputs=JOB.inputs, cancel=threading.Event()))
        runs = [call["argv"] for call in calls(log) if call["argv"][0] == "run"]
        strip = lambda argv: [part for index, part in enumerate(argv) if index == 0 or argv[index - 1] != "--name"]
        self.assertEqual(strip(runs[0]), strip(runs[1]))
        self.assertEqual(runs[1][:2], ["run", "--rm"])
        for flag in ("--network", "--read-only", "--cap-drop", "--pids-limit", "--memory", "--user"):
            self.assertIn(flag, runs[1])

    def test_cancelled_before_start_never_creates_a_container(self):
        docker, log = fake_docker(self.directory, "sys.exit(0)\n")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(SandboxCancelled):
            DockerSandboxRunner(docker=docker).run(SandboxJob(analysis_id="analysis_0001", attempt=1, code="print(1)", inputs=JOB.inputs, cancel=cancel))
        self.assertFalse(any(call["argv"][0] == "run" for call in calls(log)))

    def test_cancel_while_waiting_for_a_slot(self):
        docker, _ = fake_docker(self.directory, "sys.exit(0)\n")
        runner = DockerSandboxRunner(docker=docker, max_concurrent=1, queue_seconds=30)
        runner._slots.acquire()
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        with self.assertRaises(SandboxCancelled):
            runner.run(SandboxJob(analysis_id="analysis_0001", attempt=1, code="print(1)", inputs=JOB.inputs, cancel=cancel))
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
