"""Story 3.1/3.2: typed progress, real cancellation, cleanup and cancel/complete races through HTTP."""
import json
import logging
import threading
import time
import unittest

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.adapters.qwen import FakeQwenProvider, ModelTurn, RawToolCall
from app.adapters.sandbox import SandboxCancelled
from app.application.lifecycle import AnalysisJobs
from app.application.sessions import SessionStore
from app.domain.contracts import AnalysisStatus, ModelAnswer, Narrative
from app.main import create_app
from tests.support import SECRET_CELL, artifact, failed, follow, succeeded

GOOD = json.dumps({"code": 'df = table("artifact_1")\nshow_table(df, "Итоги")\n', "inputs": ["artifact_1"], "method": "Сумма."})
WAIT = 10


def tool(index=1):
    return ModelTurn(tool_calls=(RawToolCall(call_id=f"call_{index}", name="run_analysis", arguments=GOOD),))


def answer(based=False):
    return ModelTurn(content=ModelAnswer(conclusion="Вывод по расчёту.", evidence=["Таблица «Итоги»"], nextCheck="Сверьте.", basedOnComputation=based).model_dump_json())


class GatedProvider(FakeQwenProvider):
    """Scripted model turns; a turn listed in `gates` blocks until the test releases it."""

    def __init__(self, *turns, gates=()):
        self.turns = list(turns)
        self.calls = 0
        self.gates = {index: threading.Event() for index in gates}
        self.entered = {index: threading.Event() for index in gates}

    def next_turn(self, context, exchanges, tools_enabled, cancel=None):
        index = self.calls
        self.calls += 1
        if index in self.gates:
            self.entered[index].set()
            wait_gate(self.gates[index], cancel)
        return self.turns[index]


class GatedSandbox:
    """Sandbox double: scripted outcomes; a gated attempt blocks until cancelled (like the Docker runner)."""

    def __init__(self, *outcomes, gate_attempt=None):
        self.outcomes = list(outcomes)
        self.gate_attempt = gate_attempt
        self.entered = threading.Event()
        self.jobs = []
        self.cancelled_jobs = []

    def run(self, job):
        self.jobs.append(job)
        if job.attempt == self.gate_attempt:
            self.entered.set()
            if job.cancel.wait(WAIT):
                self.cancelled_jobs.append(job.attempt)
                raise SandboxCancelled()
        return self.outcomes.pop(0) if self.outcomes else succeeded()


def wait_gate(gate, cancel=None):
    """Block like a slow model call: until the test opens the gate or the analysis is cancelled."""
    deadline = time.monotonic() + WAIT
    while not gate.wait(0.005) and time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            return


def wait_until(predicate, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


class LifecycleApiTests(unittest.TestCase):
    def setUp(self):
        self.store = SessionStore()
        self.jobs = AnalysisJobs()

    def app(self, provider, sandbox=None, jobs=None):
        self.client = TestClient(create_app(self.store, lambda: provider, sandbox or GatedSandbox(), jobs if jobs is not None else self.jobs))
        return self.client

    def session(self, scope="range"):
        return self.client.post("/api/sessions", json={"scope": scope}).json()["sessionId"]

    def start(self, analysis_id="analysis_0001", question="Посчитай рост", session_id=None):
        body = {"analysisId": analysis_id, "scope": "range", "question": question, "artifact": artifact("P&L", "P&L!A1:C3", 3, 3, SECRET_CELL)}
        if session_id:
            body["sessionId"] = session_id
        return self.client.post("/api/analysis", json=body)

    def status(self, analysis_id="analysis_0001"):
        return self.client.get(f"/api/analyses/{analysis_id}").json()

    def cancel(self, analysis_id="analysis_0001"):
        response = self.client.post(f"/api/analyses/{analysis_id}/cancel")
        self.assertEqual(response.status_code, 200)
        AnalysisStatus.model_validate(response.json())
        return response.json()

    def stages_of(self, analysis_id="analysis_0001"):
        seen = []
        while True:
            body = self.status(analysis_id)
            if not seen or seen[-1] != (body["stage"], body["computationAttempt"]):
                seen.append((body["stage"], body["computationAttempt"]))
            if body["stage"] in ("completed", "failed", "cancelled"):
                return seen, body
            time.sleep(0.002)

    # ---- progress ---------------------------------------------------------------------------

    def test_start_is_asynchronous_and_reports_every_server_stage(self):
        provider = GatedProvider(tool(), tool(2), answer(True), gates=(0, 1, 2))
        sandbox = GatedSandbox(failed(), succeeded())
        self.app(provider, sandbox)
        session_id = self.session()
        response = self.start(session_id=session_id)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["stage"], "preparing_answer")
        observed = []

        def watch():
            observed.extend(self.stages_of()[0])

        watcher = threading.Thread(target=watch)
        watcher.start()
        for index in (0, 1, 2):
            provider.entered[index].wait(WAIT)
            time.sleep(0.05)
            provider.gates[index].set()
        watcher.join(WAIT)
        stages = [stage for stage, _ in observed]
        self.assertEqual(stages[0], "preparing_answer")
        self.assertIn(("retrying_computation", 2), observed)
        self.assertEqual(stages[-1], "completed")
        result = self.status()["result"]
        self.assertEqual(result["computation"]["attempts"], 2)
        self.assertEqual(len(self.store.snapshot(session_id).history), 1)

    def test_running_computation_and_finalizing_are_visible(self):
        provider = GatedProvider(tool(), answer(True))
        sandbox = GatedSandbox(gate_attempt=1)
        self.app(provider, sandbox)
        self.start()
        sandbox.entered.wait(WAIT)
        body = self.status()
        self.assertEqual((body["stage"], body["computationAttempt"], body["maxComputationAttempts"]), ("running_computation", 1, 3))
        self.assertIsNone(body["result"])
        self.cancel()

    def test_workbook_plan_reports_selecting_then_planned(self):
        provider = GatedProvider()
        gate = threading.Event()
        provider.select_ranges = lambda context, cancel=None: (wait_gate(gate, cancel), FakeQwenProvider().select_ranges(context))[1]
        self.app(provider)
        session_id = self.session("workbook")
        workbook_map = {"sheets": [{"sheetName": "P&L", "index": 0, "visibility": "Visible", "usedRangeAddress": "'P&L'!A1:C3", "rowCount": 3, "columnCount": 3, "tables": []}]}
        response = self.client.post(f"/api/sessions/{session_id}/workbook/plan", json={"analysisId": "analysis_0001", "question": "Что?", "workbookMap": workbook_map})
        self.assertEqual((response.status_code, response.json()["stage"]), (202, "selecting_artifacts"))
        gate.set()
        plan = follow(self.client, response, until_planned=True)
        self.assertEqual(plan["selections"][0]["address"], "A1:C3")
        self.assertEqual(self.status()["stage"], "planned")

    # ---- cancellation -----------------------------------------------------------------------

    def test_cancel_during_qwen_stops_further_model_calls_and_records_nothing(self):
        provider = GatedProvider(tool(), answer(True), gates=(0,))
        sandbox = GatedSandbox()
        self.app(provider, sandbox)
        session_id = self.session()
        self.start(session_id=session_id)
        provider.entered[0].wait(WAIT)
        started = time.monotonic()
        body = self.cancel()
        self.assertLess(time.monotonic() - started, 2, "an in-flight Qwen call must not delay the cancel")
        self.assertEqual(body["stage"], "cancelled")
        self.assertEqual(body["result"]["status"], "cancelled")
        self.assertNotIn("narrative", body["result"])
        provider.gates[0].set()  # the late model reply arrives after the cancel
        time.sleep(0.3)
        self.assertEqual(provider.calls, 1, "no further Qwen request after cancel")
        self.assertEqual(sandbox.jobs, [], "the late tool call never reaches the sandbox")
        self.assertEqual(self.status()["stage"], "cancelled")
        self.assertEqual(self.store.snapshot(session_id).history, ())

    def test_cancel_during_sandbox_terminates_the_job(self):
        provider = GatedProvider(tool(), answer(True))
        sandbox = GatedSandbox(gate_attempt=1)
        self.app(provider, sandbox)
        self.start()
        sandbox.entered.wait(WAIT)
        body = self.cancel()
        self.assertEqual(body["stage"], "cancelled")
        self.assertEqual(sandbox.cancelled_jobs, [1], "the running sandbox job received the termination signal")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(self.jobs.active_count(), 0)

    def test_cancel_during_retry(self):
        provider = GatedProvider(tool(), tool(2), answer(True))
        sandbox = GatedSandbox(failed(), gate_attempt=2)
        self.app(provider, sandbox)
        self.start()
        sandbox.entered.wait(WAIT)
        self.assertEqual((self.status()["stage"], self.status()["computationAttempt"]), ("retrying_computation", 2))
        body = self.cancel()
        self.assertEqual(body["stage"], "cancelled")
        self.assertEqual(sandbox.cancelled_jobs, [2])
        self.assertEqual(provider.calls, 2, "the model is not asked again after the cancelled retry")

    def test_cancel_right_before_the_ready_answer_never_returns_it(self):
        provider = GatedProvider(tool(), answer(True), gates=(1,))
        self.app(provider, GatedSandbox(succeeded()))
        session_id = self.session()
        self.start(session_id=session_id)
        provider.entered[1].wait(WAIT)  # computation done, final answer is being produced
        self.assertEqual(self.cancel()["stage"], "cancelled")
        provider.gates[1].set()
        time.sleep(0.3)
        body = self.status()
        self.assertEqual(body["stage"], "cancelled")
        self.assertEqual(set(body["result"]), {"analysisId", "status", "message", "nextAction", "elapsedMs"})
        self.assertEqual(self.store.snapshot(session_id).history, ())

    def test_cancel_after_commit_is_refused_and_the_answer_stays_completed(self):
        provider = GatedProvider(answer())
        self.app(provider)
        session_id = self.session()
        publish_entered, release = threading.Event(), threading.Event()
        original = self.store.record_turn

        def slow_record(*args):
            publish_entered.set()
            release.wait(WAIT)
            original(*args)

        self.store.record_turn = slow_record
        self.start(session_id=session_id)
        publish_entered.wait(WAIT)
        outcome = {}
        canceller = threading.Thread(target=lambda: outcome.update(self.cancel()))
        canceller.start()
        time.sleep(0.1)
        release.set()
        canceller.join(WAIT)
        self.assertIn(outcome["stage"], ("finalizing", "completed"))
        _, final = self.stages_of()
        self.assertEqual(final["stage"], "completed")
        self.assertEqual(len(self.store.snapshot(session_id).history), 1)

    def test_cancel_after_completion_reports_completed(self):
        self.app(GatedProvider(answer()))
        follow(self.client, self.start())
        self.assertEqual(self.cancel()["stage"], "completed")

    def test_cancel_before_start_prevents_the_late_start(self):
        provider = GatedProvider(answer())
        self.app(provider)
        self.assertEqual(self.cancel("analysis_early")["stage"], "cancelled")
        response = self.start("analysis_early")
        self.assertEqual((response.status_code, response.json()["stage"]), (200, "cancelled"))
        self.assertEqual(provider.calls, 0)

    def test_cancel_planned_workbook_analysis_drops_the_plan(self):
        provider = FakeQwenProvider()
        self.app(provider)
        session_id = self.session("workbook")
        workbook_map = {"sheets": [{"sheetName": "P&L", "index": 0, "visibility": "Visible", "usedRangeAddress": "'P&L'!A1:C3", "rowCount": 3, "columnCount": 3, "tables": []}]}
        plan = follow(self.client, self.client.post(f"/api/sessions/{session_id}/workbook/plan", json={"analysisId": "analysis_0001", "question": "Что?", "workbookMap": workbook_map}), until_planned=True)
        self.assertEqual(self.cancel()["stage"], "cancelled")
        self.assertEqual(self.store._sessions[session_id].pending_plans, {})
        artifacts = [artifact("P&L", "'P&L'!A1:C3", 3, 3)]
        self.assertTrue(plan["selections"])
        late = self.client.post(f"/api/sessions/{session_id}/workbook/analysis", json={"analysisId": "analysis_0001", "artifacts": artifacts})
        self.assertEqual(late.json()["stage"], "cancelled")
        self.assertEqual(self.store.snapshot(session_id).history, ())

    def test_cleanup_after_cancel_leaves_no_job_state(self):
        provider = GatedProvider(tool(), answer(True))
        sandbox = GatedSandbox(gate_attempt=1)
        self.app(provider, sandbox)
        session_id = self.session()
        self.start(session_id=session_id)
        sandbox.entered.wait(WAIT)
        self.cancel()
        job = self.jobs._jobs["analysis_0001"]
        self.assertIsNone(job.control)
        self.assertIsNone(job.plan)
        self.assertIsNone(job.timer)
        self.assertEqual(job.on_cancel, [])
        self.assertEqual(job.result.status, "cancelled")
        session = self.store._sessions[session_id]
        self.assertEqual((session.history, session.artifacts, session.pending_plans), ([], [], {}))
        self.assertNotIn(SECRET_CELL, json.dumps(self.status()))

    def test_closing_the_session_cancels_its_running_analysis(self):
        provider = GatedProvider(answer(), gates=(0,))
        self.app(provider)
        session_id = self.session()
        self.start(session_id=session_id)
        provider.entered[0].wait(WAIT)
        self.assertEqual(self.client.delete(f"/api/sessions/{session_id}").status_code, 204)
        wait_until(lambda: self.status()["stage"] == "cancelled")
        provider.gates[0].set()

    def test_cancel_logs_contain_only_metadata(self):
        provider = GatedProvider(answer(), gates=(0,))
        self.app(provider)
        with self.assertLogs("app", level=logging.INFO) as logs:
            self.start(question="Секретный вопрос о марже")
            provider.entered[0].wait(WAIT)
            self.cancel()
        text = "\n".join(logs.output)
        self.assertIn("analysis_cancelled analysisId=analysis_0001 reason=user", text)
        for forbidden in (SECRET_CELL, "Секретный", "P&L", "show_table"):
            self.assertNotIn(forbidden, text)
        provider.gates[0].set()

    # ---- typed failures ---------------------------------------------------------------------

    def test_timeout_is_a_typed_failure_with_cleanup(self):
        provider = GatedProvider(answer(), gates=(0,))
        self.app(provider, jobs=AnalysisJobs(timeout_seconds=0.3))
        self.start()
        _, body = self.stages_of()
        self.assertEqual(body["result"]["error"]["code"], "ANALYSIS_TIMEOUT")
        provider.gates[0].set()

    def test_capacity_and_conflict_are_typed(self):
        provider = GatedProvider(answer(), answer(), gates=(0,))
        self.app(provider, jobs=AnalysisJobs(max_active=1))
        self.start("analysis_0001")
        provider.entered[0].wait(WAIT)
        busy = self.start("analysis_0002")
        self.assertEqual((busy.status_code, busy.json()["result"]["error"]["code"]), (503, "ANALYSIS_CAPACITY"))
        again = self.start("analysis_0001")
        self.assertEqual((again.status_code, again.json()["result"]["error"]["code"]), (409, "ANALYSIS_CONFLICT"))
        provider.gates[0].set()

    def test_unknown_analysis_status_is_not_found(self):
        self.app(GatedProvider())
        response = self.client.get("/api/analyses/analysis_missing")
        self.assertEqual((response.status_code, response.json()["result"]["error"]["code"]), (404, "ANALYSIS_NOT_FOUND"))
        self.assertEqual(self.client.get("/api/analyses/bad id!").status_code, 422)

    def test_internal_errors_never_leak_their_message(self):
        class Exploding(FakeQwenProvider):
            def next_turn(self, *args, **kwargs):
                raise RuntimeError(f"/srv/finlens/secret.py {SECRET_CELL}")

        self.app(Exploding())
        with self.assertLogs("app", level=logging.INFO) as logs:
            result = follow(self.client, self.start())
        self.assertEqual(result["error"]["code"], "INTERNAL_ERROR")
        text = json.dumps(result, ensure_ascii=False) + "\n".join(logs.output)
        for forbidden in (SECRET_CELL, "/srv/finlens", "Traceback"):
            self.assertNotIn(forbidden, text)
        self.assertIn("errorType=RuntimeError", text)

    def test_invalid_context_never_reaches_qwen(self):
        provider = GatedProvider(answer())
        self.app(provider)
        empty = self.client.post("/api/analysis", json={"analysisId": "analysis_0001", "scope": "range", "question": "Что?", "artifact": {"sheetName": "P&L", "address": "A1", "values": [], "formulas": [], "numberFormats": []}})
        self.assertEqual(empty.status_code, 422)
        too_big = self.client.post("/api/analysis", json={"analysisId": "analysis_0002", "scope": "range", "question": "Что?", "artifact": artifact("P&L", "P&L!A1:A10001", 10_001, 1)})
        self.assertEqual(too_big.status_code, 422)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(len(self.jobs), 0)


class InsufficientDataTests(unittest.TestCase):
    def test_insufficient_data_must_name_what_is_missing(self):
        with self.assertRaises(ValidationError):
            Narrative(conclusion="Нельзя подтвердить.", evidence=["—"], nextCheck="—", insufficientData=True)
        narrative = Narrative(conclusion="Нельзя подтвердить.", evidence=["—"], nextCheck="—", insufficientData=True, missingData=["Данные 2024"])
        self.assertTrue(narrative.insufficientData)

    def test_fake_insufficient_scenario_is_flagged_not_masked(self):
        client = TestClient(create_app(SessionStore(), FakeQwenProvider, GatedSandbox(), AnalysisJobs()))
        body = {"analysisId": "analysis_0001", "scope": "range", "question": "Сравни годы [fake:insufficient]", "artifact": artifact("P&L", "P&L!A1:B2", 2, 2)}
        result = follow(client, client.post("/api/analysis", json=body))
        self.assertTrue(result["narrative"]["insufficientData"])
        self.assertTrue(result["narrative"]["missingData"])
        self.assertIn("нельзя", result["narrative"]["conclusion"])


if __name__ == "__main__":
    unittest.main()
