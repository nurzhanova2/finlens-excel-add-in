"""Story 2.2/2.3: retries with safe diagnostics, honest failure and execution truth before narrative."""
import json
import logging
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.adapters.qwen import AnswerContext, FakeQwenProvider, HttpQwenProvider, ModelTurn, RawToolCall, build_qwen_provider
from app.adapters.sandbox import SandboxBusy, SandboxUnavailable, UnavailableSandboxRunner
from app.application.agent import run_agent, sanitize_diagnostic
from app.application.errors import AnalysisFailure
from app.application.sessions import SessionStore
from app.domain.computation import MAX_EXECUTION_ATTEMPTS, SafeDiagnostic
from app.domain.contracts import ModelAnswer, TabularArtifact
from app.main import create_app
from tests.support import SECRET_CELL, CodeAwareSandbox, FakeQwenServer, ScriptedSandbox, artifact, chat_reply, failed, follow, succeeded

GOOD = json.dumps({"code": 'df = table("artifact_1")\nshow_table(df, "Итоги")\n', "inputs": ["artifact_1"], "method": "Сумма по столбцам."})


def answer(based=False, conclusion="EBITDA выросла на 10% (расчёт)."):
    return ModelTurn(content=ModelAnswer(conclusion=conclusion, evidence=["Таблица «Итоги»"], nextCheck="Сверьте итоги.", basedOnComputation=based).model_dump_json())


def tool(arguments=GOOD, index=1):
    return ModelTurn(tool_calls=(RawToolCall(call_id=f"call_{index}", name="run_analysis", arguments=arguments),))


class ScriptedProvider:
    """Returns scripted model turns and records what the model was shown."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls = []

    def next_turn(self, context, exchanges, tools_enabled, cancel=None):
        self.calls.append((exchanges, tools_enabled))
        return self.turns.pop(0)


def context(*artifacts, question="Посчитай рост EBITDA"):
    items = artifacts or (TabularArtifact.model_validate(artifact("P&L", "'P&L'!A1:C3", 3, 3, SECRET_CELL)),)
    return AnswerContext(analysis_id="analysis_0001", scope="range", question=question, artifacts=tuple(items))


class RetryTests(unittest.TestCase):
    def test_plain_answer_has_no_computation(self):
        sandbox = ScriptedSandbox()
        outcome = run_agent(context(), ScriptedProvider(answer(conclusion="В диапазоне 3 строки.")), sandbox)
        self.assertIsNone(outcome.computation)
        self.assertEqual(sandbox.jobs, [])

    def test_success_on_first_attempt_returns_only_executed_code(self):
        provider = ScriptedProvider(tool(), answer(based=True))
        outcome = run_agent(context(), provider, ScriptedSandbox(succeeded()))
        computation = outcome.computation
        self.assertEqual(computation.attempts, 1)
        self.assertEqual(computation.code, json.loads(GOOD)["code"])
        self.assertEqual(computation.method, "Сумма по столбцам.")
        self.assertEqual([i.model_dump() for i in computation.inputs], [{"sheetName": "P&L", "address": "'P&L'!A1:C3"}])
        # After success the model must answer: tools are disabled and it never sees PNG bytes.
        exchanges, tools_enabled = provider.calls[1]
        self.assertFalse(tools_enabled)
        self.assertNotIn("pngBase64", json.dumps(exchanges[0].result))
        self.assertEqual(exchanges[0].result["tables"][0]["rows"][0], ["EBITDA", 220.0])

    def test_success_after_correction_uses_a_new_job_and_only_safe_diagnostic(self):
        broken = json.dumps({"code": "show_table(x, 't')", "inputs": ["artifact_1"], "method": "m"})
        leaky = failed("RUNTIME_ERROR", "KeyError", 'File "/opt/finlens/finlens_runner.py", line 9 /home/api/.env FINLENS_QWEN_API_KEY=abc Bearer xyz', 4)
        sandbox = ScriptedSandbox(leaky, succeeded())
        provider = ScriptedProvider(tool(broken), tool(GOOD, 2), answer(based=True))
        outcome = run_agent(context(), provider, sandbox)
        self.assertEqual(outcome.computation.attempts, 2)
        self.assertEqual(outcome.computation.code, json.loads(GOOD)["code"])
        self.assertEqual([job.attempt for job in sandbox.jobs], [1, 2])
        self.assertIsNot(sandbox.jobs[0], sandbox.jobs[1])
        diagnostic = provider.calls[1][0][0].result
        self.assertEqual(diagnostic["status"], "failed")
        self.assertEqual(diagnostic["attemptsRemaining"], 2)
        text = json.dumps(diagnostic, ensure_ascii=False)
        for forbidden in ("/opt/finlens", "/home/api", "FINLENS_QWEN_API_KEY", "xyz", SECRET_CELL):
            self.assertNotIn(forbidden, text)
        self.assertEqual(diagnostic["availableInputs"][0]["rows"], 3)

    def test_three_failures_end_with_typed_failure_and_no_narrative(self):
        sandbox = ScriptedSandbox(failed(), failed(), failed())
        provider = ScriptedProvider(tool(), tool(index=2), tool(index=3), answer(based=True))
        with self.assertRaises(AnalysisFailure) as error:
            run_agent(context(), provider, sandbox)
        self.assertEqual(error.exception.code, "COMPUTATION_FAILED")
        self.assertEqual(error.exception.attempts, MAX_EXECUTION_ATTEMPTS)
        self.assertIn("меньший диапазон", error.exception.next_action)
        self.assertEqual(len(sandbox.jobs), 3)
        # The model is not asked again: no narrative can be produced after the limit.
        self.assertEqual(len(provider.calls), 3)

    def test_rejected_tool_calls_count_as_attempts_and_never_reach_the_sandbox(self):
        bad = json.dumps({"code": "import os\nos.system('id')", "inputs": ["artifact_1"], "method": "m"})
        sandbox = ScriptedSandbox()
        provider = ScriptedProvider(tool(bad), tool("{oops", 2), tool(GOOD, 3), answer(based=True))
        outcome = run_agent(context(), provider, sandbox)
        self.assertEqual(outcome.computation.attempts, 3)
        self.assertEqual(len(sandbox.jobs), 1)
        self.assertEqual(provider.calls[1][0][0].result["error"]["code"], "TOOL_CALL_REJECTED")

    def test_tool_call_after_success_is_refused(self):
        provider = ScriptedProvider(tool(), tool(index=2))
        with self.assertRaises(AnalysisFailure) as error:
            run_agent(context(), provider, ScriptedSandbox(succeeded()))
        self.assertEqual(error.exception.code, "QWEN_INVALID_RESPONSE")

    def test_unavailable_or_busy_sandbox_fails_honestly_without_retry(self):
        for exception, code in ((SandboxUnavailable("down"), "SANDBOX_UNAVAILABLE"), (SandboxBusy(), "SANDBOX_BUSY")):
            provider = ScriptedProvider(tool(), answer(based=True))
            with self.assertRaises(AnalysisFailure) as error:
                run_agent(context(), provider, ScriptedSandbox(exception))
            self.assertEqual(error.exception.code, code)
            self.assertEqual(len(provider.calls), 1)
        with self.assertRaises(AnalysisFailure) as error:
            run_agent(context(), ScriptedProvider(tool()), UnavailableSandboxRunner())
        self.assertEqual(error.exception.code, "SANDBOX_UNAVAILABLE")

    def test_narrative_cannot_claim_a_computation_without_successful_execution(self):
        with self.assertRaises(AnalysisFailure) as error:
            run_agent(context(), ScriptedProvider(answer(based=True)), ScriptedSandbox())
        self.assertEqual(error.exception.code, "UNVERIFIED_COMPUTATION")
        # Also after a failed attempt the model cannot fall back to a "computed" answer.
        with self.assertRaises(AnalysisFailure) as error:
            run_agent(context(), ScriptedProvider(tool(), answer(based=True)), ScriptedSandbox(failed()))
        self.assertEqual(error.exception.code, "UNVERIFIED_COMPUTATION")

    def test_artifact_isolation_only_referenced_artifacts_reach_the_job(self):
        first = TabularArtifact.model_validate(artifact("P&L", "P&L!A1:B2", 2, 2))
        second = TabularArtifact.model_validate(artifact("Debt", "Debt!A1:A2", 2, 1, SECRET_CELL))
        arguments = json.dumps({"code": "print(1)", "inputs": ["artifact_1"], "method": "m"})
        sandbox = ScriptedSandbox(succeeded())
        run_agent(context(first, second), ScriptedProvider(tool(arguments), answer(based=True)), sandbox)
        self.assertEqual([item.artifact.sheetName for item in sandbox.jobs[0].inputs], ["P&L"])

    def test_invalid_final_answer_is_a_typed_failure(self):
        with self.assertRaises(AnalysisFailure) as error:
            run_agent(context(), ScriptedProvider(ModelTurn(content="Выручка выросла")), ScriptedSandbox())
        self.assertEqual(error.exception.code, "QWEN_INVALID_RESPONSE")

    def test_diagnostic_scrub_is_bounded(self):
        clean = sanitize_diagnostic(SafeDiagnostic(code="RUNTIME_ERROR", errorType="ValueError", message="C:\\Users\\a\\b.py api_key=1 " + "x" * 300))
        self.assertLessEqual(len(clean.message), 400)
        self.assertNotIn("Users", clean.message)
        self.assertNotIn("api_key", clean.message)


class FakeProviderApiTests(unittest.TestCase):
    """End-to-end through HTTP with the explicit fake provider and a code-aware sandbox double."""

    def setUp(self):
        self.sandbox = CodeAwareSandbox()
        self.client = TestClient(create_app(SessionStore(), FakeQwenProvider, self.sandbox))

    def ask(self, question, analysis_id=None):
        self.asked = getattr(self, "asked", 0) + 1
        body = {"analysisId": analysis_id or f"analysis_{self.asked:04d}", "scope": "range", "question": question, "artifact": artifact("P&L", "P&L!A1:C3", 3, 3, SECRET_CELL)}
        return follow(self.client, self.client.post("/api/analysis", json=body))

    def test_compute_retry_fail_and_invalid_tool_scenarios(self):
        computed = self.ask("Посчитай [fake:compute]")
        self.assertEqual(computed["status"], "completed")
        self.assertEqual(computed["computation"]["attempts"], 1)
        self.assertTrue(computed["computation"]["charts"][0]["pngBase64"])
        self.assertIn("Расчёт:", computed["narrative"]["evidence"][0])

        retried = self.ask("Посчитай [fake:retry]")
        self.assertEqual(retried["computation"]["attempts"], 2)
        self.assertNotIn("undefined_totals", retried["computation"]["code"])

        invalid = self.ask("Посчитай [fake:invalid-tool]")
        self.assertEqual(invalid["computation"]["attempts"], 2)
        self.assertNotIn("import os", invalid["computation"]["code"])

        failed_result = self.ask("Посчитай [fake:fail]")
        self.assertEqual(failed_result["status"], "failed")
        self.assertEqual(failed_result["error"]["code"], "COMPUTATION_FAILED")
        self.assertEqual(failed_result["computationAttempts"], 3)
        for key in ("narrative", "computation"):
            self.assertNotIn(key, failed_result)

        unverified = self.ask("Ответь [fake:unverified]")
        self.assertEqual(unverified["error"]["code"], "UNVERIFIED_COMPUTATION")

        plain = self.ask("Что в диапазоне?")
        self.assertIsNone(plain["computation"])

    def test_logs_hold_no_code_cells_question_or_png(self):
        with self.assertLogs("app", level=logging.INFO) as logs:
            self.ask("Секретный вопрос [fake:retry]", "analysis_0001")
        text = "\n".join(logs.output)
        self.assertIn("computation_attempt analysisId=analysis_0001 attempt=1 outcome=failed", text)
        self.assertIn("computed=True attempts=2", text)
        for forbidden in (SECRET_CELL, "Секретный", "undefined_totals", "show_table", "iVBOR", "P&L"):
            self.assertNotIn(forbidden, text)

    def test_without_sandbox_the_api_reports_unavailable(self):
        client = TestClient(create_app(SessionStore(), FakeQwenProvider, UnavailableSandboxRunner()))
        body = {"analysisId": "analysis_0001", "scope": "range", "question": "[fake:compute]", "artifact": artifact("P&L", "P&L!A1", 1, 1)}
        result = follow(client, client.post("/api/analysis", json=body))
        self.assertEqual(result["error"]["code"], "SANDBOX_UNAVAILABLE")

    def test_fake_provider_requires_explicit_opt_in(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(Exception) as error:
                build_qwen_provider()
            self.assertEqual(error.exception.code, "QWEN_NOT_CONFIGURED")


class HttpToolCallingTests(unittest.TestCase):
    def test_http_provider_sends_tool_schema_and_tool_results(self):
        server = FakeQwenServer(
            chat_reply(tool_calls=[{"id": "c1", "type": "function", "function": {"name": "run_analysis", "arguments": GOOD}}]),
            chat_reply(answer(based=True).content),
        )
        self.addCleanup(server.close)
        provider = HttpQwenProvider(base_url=server.base_url, api_key="server-secret", model="qwen")
        outcome = run_agent(context(), provider, ScriptedSandbox(succeeded()))
        payloads = server.requests
        self.assertEqual(outcome.computation.attempts, 1)
        self.assertEqual(server.auth, ["Bearer server-secret"] * 2)
        self.assertEqual(payloads[0]["tools"][0]["function"]["name"], "run_analysis")
        self.assertEqual(payloads[0]["tool_choice"], "auto")
        self.assertEqual(payloads[1]["tool_choice"], "none")
        roles = [message["role"] for message in payloads[1]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool"])
        self.assertEqual(payloads[1]["messages"][3]["tool_call_id"], "c1")
        self.assertIn('"ref": "artifact_1"', payloads[0]["messages"][1]["content"])
        self.assertNotIn("server-secret", json.dumps(payloads))
        self.assertNotIn("pngBase64", json.dumps(payloads))

if __name__ == "__main__":
    unittest.main()
