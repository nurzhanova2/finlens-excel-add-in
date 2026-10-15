"""Story 2.1/2.2 security and integration tests against the real hardened container.

Requires Docker and the image built from sandbox-image/ (`npm run sandbox:build`). Without them the
tests are skipped, unless FINLENS_REQUIRE_SANDBOX_TESTS=1 (`npm run test:sandbox`), which turns a
missing runtime into a failure.
"""
import json
import os
import shutil
import subprocess
import threading
import time
import unittest

from fastapi.testclient import TestClient

from app.adapters.qwen import FakeQwenProvider
from app.adapters.sandbox import DEFAULT_IMAGE, JOB_LABEL, DockerSandboxRunner, ExecutionFailed, ExecutionSucceeded, SandboxCancelled, SandboxInput, SandboxJob, SandboxLimits, build_docker_command
from app.application.sessions import SessionStore
from app.domain.contracts import TabularArtifact
from app.main import create_app
from tests.support import artifact, follow

IMAGE = os.getenv("FINLENS_SANDBOX_IMAGE", DEFAULT_IMAGE)
REQUIRED = os.getenv("FINLENS_REQUIRE_SANDBOX_TESTS") == "1"


def docker_ready() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        return subprocess.run([docker, "image", "inspect", IMAGE], capture_output=True, timeout=20, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


READY = docker_ready()
if REQUIRED and not READY:
    raise RuntimeError(f"FINLENS_REQUIRE_SANDBOX_TESTS=1 but Docker or the image {IMAGE} is unavailable; run npm run sandbox:build")

PNL = TabularArtifact.model_validate({
    "sheetName": "P&L", "address": "'P&L'!A1:C3",
    "values": [["Показатель", 2024, 2025], ["Выручка", 1000, 1250], ["EBITDA", 200, 220]],
    "formulas": [["", "", ""], ["", "", ""], ["", "", "=C2*0.176"]],
    "numberFormats": [["General"] * 3] * 3,
})


def job(code: str, attempt: int = 1) -> SandboxJob:
    return SandboxJob(analysis_id="analysis_docker", attempt=attempt, code=code, inputs=(SandboxInput("artifact_1", PNL),))


def leftover_jobs() -> list[str]:
    result = subprocess.run(["docker", "ps", "--all", "--filter", f"label={JOB_LABEL}", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=20, check=False)
    return result.stdout.split()


@unittest.skipUnless(READY, "Docker or the FinLens sandbox image is not available")
class SandboxContainerTests(unittest.TestCase):
    def setUp(self):
        self.runner = DockerSandboxRunner(image=IMAGE, limits=SandboxLimits(wall_seconds=5, cpu_seconds=5, startup_grace_seconds=15))

    def tearDown(self):
        self.assertEqual(leftover_jobs(), [], "every job container must be removed")

    def probe(self, script: str) -> dict:
        """Run a diagnostic script with exactly the production hardening flags (bypassing the runner)."""
        command = build_docker_command("docker", IMAGE, "finlens-job-probe", self.runner.limits, entrypoint=["python", "-I", "-c", script])
        # A server secret present in the API process environment must not reach the container.
        env = {**os.environ, "FINLENS_QWEN_API_KEY": "host-only-secret"}
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False, env=env)
        finally:
            subprocess.run(["docker", "rm", "--force", "finlens-job-probe"], capture_output=True, check=False)
        if not completed.stdout.strip():
            self.fail(f"probe failed: {completed.stderr[-500:]}")
        return json.loads(completed.stdout)

    def test_successful_job_returns_typed_tables_and_png(self):
        code = (
            'df = table("artifact_1")\n'
            'df = df.set_index("Показатель").apply(pd.to_numeric)\n'
            'growth = (df["2025"] / df["2024"] - 1).round(4)\n'
            'show_table(growth.rename("Рост"), "Рост 2025 к 2024")\n'
            'fig, ax = plt.subplots(figsize=(5, 3))\nax.bar(growth.index, growth.values)\n'
            'show_chart(fig, title="Рост", metric="Рост год к году", period="2024–2025", insight=f"Максимальный рост: {growth.idxmax()} {growth.max():.1%}")\n'
        )
        outcome = self.runner.run(job(code))
        self.assertIsInstance(outcome, ExecutionSucceeded, getattr(outcome, "diagnostic", None))
        table = outcome.output.tables[0]
        self.assertEqual(table.rows, [["Выручка", 0.25], ["EBITDA", 0.1]])
        chart = outcome.output.charts[0]
        self.assertEqual(chart.description.insight, "Максимальный рост: Выручка 25.0%")
        self.assertEqual((chart.widthPx, chart.heightPx), (500, 300))

    def test_container_is_non_root_unprivileged_and_isolated(self):
        result = self.probe(
            "import json, os, socket\n"
            "status = dict(line.split(':\\t', 1) for line in open('/proc/self/status').read().splitlines() if ':\\t' in line)\n"
            "def attempt(action):\n"
            "    try:\n        action(); return 'allowed'\n"
            "    except Exception as error:\n        return type(error).__name__\n"
            "def connect():\n    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
            "def resolve():\n    socket.getaddrinfo('example.com', 443)\n"
            "def write_root():\n    open('/opt/finlens/pwned', 'w').write('x')\n"
            "def write_etc():\n    open('/etc/pwned', 'w').write('x')\n"
            "def exec_tmp():\n"
            "    open('/tmp/x.sh', 'w').write('#!/bin/sh\\necho hi\\n'); os.chmod('/tmp/x.sh', 0o755); os.execv('/tmp/x.sh', ['/tmp/x.sh'])\n"
            "def setuid():\n    os.setuid(0)\n"
            "mounts = open('/proc/self/mounts').read()\n"
            "print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), 'capEff': status['CapEff'].strip(), 'noNewPrivs': status['NoNewPrivs'].strip(),"
            " 'connect': attempt(connect), 'resolve': attempt(resolve), 'writeRoot': attempt(write_root), 'writeEtc': attempt(write_etc),"
            " 'execTmp': attempt(exec_tmp), 'setuid': attempt(setuid), 'routes': open('/proc/net/route').read().splitlines()[1:],"
            " 'up': sorted(n for n in os.listdir('/sys/class/net') if os.path.isdir(f'/sys/class/net/{n}') and open(f'/sys/class/net/{n}/operstate').read().strip() not in ('down',) and n != 'lo'),"
            " 'secrets': sorted(k for k in os.environ if any(m in k for m in ('FINLENS', 'QWEN', 'API_KEY', 'TOKEN', 'SECRET', 'PASSWORD'))),"
            " 'leak': any('host-only-secret' in v for v in os.environ.values()),"
            " 'dockerSock': os.path.exists('/var/run/docker.sock'), 'hostMounts': [l.split()[1] for l in mounts.splitlines() if l.split()[1].startswith(('/Users', '/home', '/host', '/mnt', '/var/run'))]}))\n"
        )
        self.assertNotEqual(result["uid"], 0)
        self.assertEqual(result["uid"], 10001)
        self.assertEqual(result["capEff"], "0000000000000000")
        self.assertEqual(result["noNewPrivs"], "1")
        self.assertNotEqual(result["connect"], "allowed")
        self.assertNotEqual(result["resolve"], "allowed")
        # --network none: no routes and no active interface except loopback.
        self.assertEqual(result["routes"], [])
        self.assertEqual(result["up"], [])
        self.assertNotEqual(result["writeRoot"], "allowed")
        self.assertNotEqual(result["writeEtc"], "allowed")
        self.assertEqual(result["execTmp"], "PermissionError")
        self.assertNotEqual(result["setuid"], "allowed")
        self.assertEqual(result["secrets"], [])
        self.assertFalse(result["leak"])
        self.assertFalse(result["dockerSock"])
        self.assertEqual(result["hostMounts"], [])

    def test_only_allowlisted_libraries_are_installed(self):
        result = self.probe(
            "import json, importlib.util\n"
            "names = ['pandas', 'numpy', 'matplotlib', 'requests', 'urllib3', 'scipy', 'pip', 'openpyxl', 'httpx', 'boto3']\n"
            "print(json.dumps({n: importlib.util.find_spec(n) is not None for n in names}))\n"
        )
        self.assertEqual(result, {"pandas": True, "numpy": True, "matplotlib": True, "requests": False, "urllib3": False, "scipy": False, "pip": False, "openpyxl": False, "httpx": False, "boto3": False})

    def test_attack_attempts_through_the_runner_fail_safely(self):
        # These bypass the API static policy on purpose: the runner and the container must still block them.
        attacks = {
            "import socket": "IMPORT_NOT_ALLOWED",
            "import os": "IMPORT_NOT_ALLOWED",
            "import subprocess": "IMPORT_NOT_ALLOWED",
            "import urllib.request": "IMPORT_NOT_ALLOWED",
            "open('/etc/passwd')": "RUNTIME_ERROR",
            "__import__('os')": "IMPORT_NOT_ALLOWED",
            "eval('1')": "RUNTIME_ERROR",
            "pd.DataFrame({'a': [1]}).to_csv('/opt/finlens/x.csv')": "RUNTIME_ERROR",
            "pd.read_csv('/proc/self/environ')": "NO_OUTPUT_OR_ERROR",
        }
        for code, expected in attacks.items():
            with self.subTest(code=code):
                outcome = self.runner.run(job(code))
                self.assertIsInstance(outcome, ExecutionFailed)
                if expected != "NO_OUTPUT_OR_ERROR":
                    self.assertEqual(outcome.diagnostic.code, expected)
                self.assertNotIn("/opt/finlens", outcome.diagnostic.message)
        # Network through an allowed library: still no route out of the container.
        outcome = self.runner.run(job("pd.read_csv('http://1.1.1.1/x.csv')"))
        self.assertIsInstance(outcome, ExecutionFailed)

    def test_wall_time_cpu_memory_and_output_limits(self):
        spinning = self.runner.run(job("while True:\n    pass\n"))
        self.assertIn(spinning.diagnostic.code, {"TIMEOUT", "CPU_LIMIT"})
        # Code that swallows the in-container alarm and soft CPU signal is still terminated.
        stubborn = self.runner.run(job("while True:\n    try:\n        while True:\n            pass\n    except BaseException:\n        pass\n"))
        self.assertIn(stubborn.diagnostic.code, {"TIMEOUT", "CPU_LIMIT"})
        self.assertLess(stubborn.duration_ms, 25_000)
        memory = self.runner.run(job("x = np.ones((400_000_000,))\nprint(x.sum())\n"))
        self.assertEqual(memory.diagnostic.code, "MEMORY_LIMIT")
        tables = self.runner.run(job("for i in range(6):\n    show_table(pd.DataFrame({'a': [i]}), f't{i}')\n"))
        self.assertEqual(tables.diagnostic.code, "OUTPUT_LIMIT")
        big = self.runner.run(job("show_table(pd.DataFrame({'a': range(1000)}), 'big')\n"))
        self.assertIsInstance(big, ExecutionSucceeded)
        self.assertEqual((len(big.output.tables[0].rows), big.output.tables[0].totalRows, big.output.tables[0].truncated), (200, 1000, True))
        wide = self.runner.run(job("show_table(pd.DataFrame([[1] * 31]), 'wide')\n"))
        self.assertEqual(wide.diagnostic.code, "OUTPUT_LIMIT")

    def test_each_attempt_is_a_fresh_container(self):
        first = self.runner.run(job("pd.DataFrame({'a': [1]}).to_csv('/tmp/state.csv')\nprint('written')\n"))
        self.assertIsInstance(first, ExecutionSucceeded)
        second = self.runner.run(job("print(pd.read_csv('/tmp/state.csv'))\n", attempt=2))
        self.assertIsInstance(second, ExecutionFailed)
        self.assertEqual(second.diagnostic.errorType, "FileNotFoundError")

    def test_cancel_terminates_a_running_container(self):
        """Story 3.1: a cancelled job is force-removed while it runs; nothing is left behind (checked in tearDown)."""
        cancel = threading.Event()
        running = SandboxJob(analysis_id="analysis_docker", attempt=1, code="total = 0\nfor value in range(10 ** 10):\n    total += value\n", inputs=job("").inputs, cancel=cancel)
        seen_running = []

        def cancel_when_started():
            deadline = time.monotonic() + 30
            while not leftover_jobs() and time.monotonic() < deadline:
                time.sleep(0.2)
            seen_running.append(bool(leftover_jobs()))
            time.sleep(0.5)
            cancel.set()

        threading.Thread(target=cancel_when_started, daemon=True).start()
        started = time.monotonic()
        with self.assertRaises(SandboxCancelled):
            self.runner.run(running)
        self.assertEqual(seen_running, [True])
        # Cancelled well before the 5 s wall-time limit plus start-up grace would have ended it.
        self.assertLess(time.monotonic() - started, 18)

    def test_api_retry_flow_with_real_sandbox(self):
        client = TestClient(create_app(SessionStore(), FakeQwenProvider, self.runner))
        body = {"analysisId": "analysis_docker", "scope": "range", "question": "Посчитай [fake:retry]", "artifact": artifact("P&L", "P&L!A1:B3", 3, 2, 10)}
        body["artifact"]["values"][0] = ["Год", "Выручка"]
        result = follow(client, client.post("/api/analysis", json=body), timeout=120)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["computation"]["attempts"], 2)
        self.assertEqual(result["computation"]["tables"][0]["rows"][0][:2], ["Год", 20.0])
        failed = follow(client, client.post("/api/analysis", json={**body, "analysisId": body["analysisId"] + "_2", "question": "Посчитай [fake:fail]"}), timeout=180)
        self.assertEqual((failed["status"], failed["error"]["code"], failed["computationAttempts"]), ("failed", "COMPUTATION_FAILED", 3))


if __name__ == "__main__":
    unittest.main()
