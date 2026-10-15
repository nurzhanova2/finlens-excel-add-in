"""Cancelling an analysis aborts the in-flight Qwen HTTP request itself, not just its result.

The HTTP adapter talks to a real (fake) OpenAI-compatible server on 127.0.0.1 that holds the request open,
so the tests can observe the client closing the connection.
"""
import json
import logging
import threading
import time
import unittest

from fastapi.testclient import TestClient

from app.adapters.qwen import AnswerContext, HttpQwenProvider, QwenProviderError, SelectionContext
from app.application.lifecycle import AnalysisJobs
from app.application.sessions import SessionStore
from app.domain.cancellation import CallCancelled, CancelSignal
from app.domain.contracts import ModelAnswer, TabularArtifact, WorkbookMap
from app.main import create_app
from tests.support import SECRET_CELL, FakeQwenServer, ScriptedSandbox, artifact, chat_reply, follow

WAIT = 10
API_KEY = "test-only-server-key"
QUESTION = "Секретный вопрос о марже"
ANSWER = ModelAnswer(conclusion="Маржа выросла.", evidence=["Строка 3"], nextCheck="Сверьте.").model_dump_json()
WORKBOOK_MAP = {"sheets": [{"sheetName": "P&L", "index": 0, "visibility": "Visible", "usedRangeAddress": "'P&L'!A1:C3", "rowCount": 3, "columnCount": 3, "tables": []}]}


def wait_until(predicate, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


def model_call_threads(exclude=frozenset()):
    return [thread for thread in threading.enumerate() if thread.name == "finlens-model-call" and thread.is_alive() and thread.ident not in exclude]


def answer_context():
    return AnswerContext(analysis_id="analysis_0001", scope="range", question=QUESTION, artifacts=(TabularArtifact.model_validate(artifact("P&L", "'P&L'!A1:C3", 3, 3, SECRET_CELL)),))


class HttpAdapterCancellationTests(unittest.TestCase):
    def server(self, *replies, status=200):
        server = FakeQwenServer(*replies, status=status)
        self.addCleanup(server.close)
        return server

    def provider(self, server, timeout_seconds=5.0):
        return HttpQwenProvider(base_url=server.base_url, api_key=API_KEY, model="qwen", timeout_seconds=timeout_seconds)

    def test_cancel_aborts_the_in_flight_request(self):
        server = self.server(FakeQwenServer.HOLD)
        provider, cancel, outcome = self.provider(server), CancelSignal(), {}

        def call():
            try:
                outcome["value"] = provider.next_turn(answer_context(), (), True, cancel)
            except BaseException as error:
                outcome["error"] = error

        caller = threading.Thread(target=call)
        caller.start()
        self.assertTrue(server.held.wait(WAIT))
        started = time.monotonic()
        cancel.set()
        caller.join(WAIT)
        self.assertFalse(caller.is_alive())
        self.assertLess(time.monotonic() - started, 1, "the blocked read returns as soon as the socket is shut down")
        self.assertIsInstance(outcome.get("error"), CallCancelled)
        self.assertTrue(server.disconnected.wait(2), "the Qwen server sees the client close the request")

    def test_cancel_aborts_workbook_selection_request(self):
        server = self.server(FakeQwenServer.HOLD)
        provider, cancel, outcome = self.provider(server), CancelSignal(), {}
        context = SelectionContext(analysis_id="analysis_0001", question=QUESTION, workbook_map=WorkbookMap.model_validate(WORKBOOK_MAP))
        caller = threading.Thread(target=lambda: outcome.update(error=self._raises(lambda: provider.select_ranges(context, cancel))))
        caller.start()
        self.assertTrue(server.held.wait(WAIT))
        cancel.set()
        caller.join(WAIT)
        self.assertIsInstance(outcome["error"], CallCancelled)
        self.assertTrue(server.disconnected.wait(2))

    def test_already_cancelled_call_sends_nothing(self):
        server = self.server(chat_reply(ANSWER))
        cancel = CancelSignal()
        cancel.set()
        with self.assertRaises(CallCancelled):
            self.provider(server).next_turn(answer_context(), (), True, cancel)
        time.sleep(0.1)
        self.assertEqual(server.requests, [])

    def test_timeout_stays_a_typed_error_without_secrets(self):
        server = self.server(FakeQwenServer.HOLD)
        with self.assertRaises(QwenProviderError) as raised:
            self.provider(server, timeout_seconds=0.3).next_turn(answer_context(), (), True, CancelSignal())
        self.assertEqual(raised.exception.code, "QWEN_UNAVAILABLE")
        for text in (raised.exception.message, raised.exception.next_action):
            self.assertNotIn(API_KEY, text)
            self.assertNotIn("127.0.0.1", text)

    def test_http_error_status_is_typed(self):
        server = self.server(chat_reply(ANSWER), status=500)
        with self.assertRaises(QwenProviderError) as raised:
            self.provider(server).next_turn(answer_context(), (), True)
        self.assertEqual(raised.exception.code, "QWEN_UNAVAILABLE")

    def test_unsupported_endpoint_urls_are_rejected_before_any_request(self):
        for base_url in ("ftp://qwen.example/v1", "https://user:pass@qwen.example/v1", "qwen.example/v1"):
            with self.subTest(base_url=base_url), self.assertRaises(QwenProviderError) as raised:
                HttpQwenProvider(base_url=base_url, api_key=API_KEY, model="qwen").next_turn(answer_context(), (), True)
            self.assertEqual(raised.exception.code, "QWEN_NOT_CONFIGURED")

    def test_cancel_signal_callbacks_run_once_and_can_unregister(self):
        signal, calls = CancelSignal(), []
        signal.on_cancel(lambda: calls.append("a"))
        unregister = signal.on_cancel(lambda: calls.append("b"))
        unregister()
        signal.set()
        signal.set()
        signal.on_cancel(lambda: calls.append("late"))
        self.assertEqual(calls, ["a", "late"])

    @staticmethod
    def _raises(fn):
        try:
            fn()
        except BaseException as error:
            return error
        return None


class AnalysisCancellationOverHttpTests(unittest.TestCase):
    def setUp(self):
        self.store = SessionStore()
        self.jobs = AnalysisJobs()
        self.server = FakeQwenServer(FakeQwenServer.HOLD)
        self.addCleanup(self.server.close)
        provider = HttpQwenProvider(base_url=self.server.base_url, api_key=API_KEY, model="qwen", timeout_seconds=30)
        self.client = TestClient(create_app(self.store, lambda: provider, ScriptedSandbox(), self.jobs))
        self.before = {thread.ident for thread in model_call_threads()}

    def session(self, scope="range"):
        return self.client.post("/api/sessions", json={"scope": scope}).json()["sessionId"]

    def start(self, session_id, analysis_id="analysis_0001"):
        body = {"analysisId": analysis_id, "scope": "range", "question": QUESTION, "sessionId": session_id, "artifact": artifact("P&L", "P&L!A1:C3", 3, 3, SECRET_CELL)}
        response = self.client.post("/api/analysis", json=body)
        self.assertEqual(response.status_code, 202)
        return response

    def cancel(self, analysis_id="analysis_0001"):
        response = self.client.post(f"/api/analyses/{analysis_id}/cancel")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def status(self, analysis_id="analysis_0001"):
        return self.client.get(f"/api/analyses/{analysis_id}").json()

    def test_cancel_during_qwen_closes_the_request_and_cleans_up(self):
        session_id = self.session()
        self.start(session_id)
        self.assertTrue(self.server.held.wait(WAIT))
        started = time.monotonic()
        body = self.cancel()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((body["stage"], body["result"]["status"]), ("cancelled", "cancelled"))
        self.assertNotIn("narrative", body["result"])
        # The worker waited for the HTTP call to unwind before reporting `cancelled`.
        self.assertEqual(model_call_threads(self.before), [], "no Qwen call thread survives the cancel")
        self.assertTrue(self.server.disconnected.wait(2), "the Qwen request was closed, not left running")
        job = self.jobs._jobs["analysis_0001"]
        self.assertIsNone(job.control)
        self.assertIsNone(job.timer)
        self.assertEqual(job.on_cancel, [])
        self.assertEqual(self.jobs.active_count(), 0)
        session = self.store._sessions[session_id]
        self.assertEqual((session.history, session.artifacts), ([], []))
        # A reply the server tries to send afterwards changes nothing.
        self.server.release(chat_reply(ANSWER))
        time.sleep(0.3)
        self.assertEqual(self.status()["stage"], "cancelled")
        self.assertEqual(self.store.snapshot(session_id).history, ())
        self.assertEqual(len(self.server.requests), 1, "no further Qwen request after cancel")

    def test_new_question_after_cancel_works(self):
        session_id = self.session()
        self.start(session_id)
        self.assertTrue(self.server.held.wait(WAIT))
        self.assertEqual(self.cancel()["stage"], "cancelled")
        self.server.replies.append(chat_reply(ANSWER))
        result = follow(self.client, self.start(session_id, "analysis_0002"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["narrative"]["conclusion"], "Маржа выросла.")
        history = self.store.snapshot(session_id).history
        self.assertEqual(len(history), 1, "only the second, completed question is in the dialog")
        self.assertEqual(self.status()["stage"], "cancelled")

    def test_cancel_during_workbook_selection_closes_the_request_and_drops_the_plan(self):
        session_id = self.session("workbook")
        response = self.client.post(f"/api/sessions/{session_id}/workbook/plan", json={"analysisId": "analysis_0001", "question": QUESTION, "workbookMap": WORKBOOK_MAP})
        self.assertEqual(response.json()["stage"], "selecting_artifacts")
        self.assertTrue(self.server.held.wait(WAIT))
        self.assertEqual(self.cancel()["stage"], "cancelled")
        self.assertTrue(self.server.disconnected.wait(2))
        self.assertEqual(self.store._sessions[session_id].pending_plans, {})

    def test_cancel_logs_have_metadata_only(self):
        session_id = self.session()
        with self.assertLogs("app", level=logging.INFO) as logs:
            self.start(session_id)
            self.assertTrue(self.server.held.wait(WAIT))
            self.cancel()
        text = "\n".join(logs.output)
        self.assertIn("model_call_aborted analysisId=analysis_0001", text)
        for forbidden in (API_KEY, "Bearer", SECRET_CELL, "Секретный", self.server.base_url):
            self.assertNotIn(forbidden, text)


class CompletedCancelRaceTests(unittest.TestCase):
    """Reply and cancel arrive together: whatever wins, the outcome is one consistent state, never both."""

    ROUNDS = 12

    def test_race_has_exactly_one_consistent_outcome(self):
        outcomes = []
        for round_index in range(self.ROUNDS):
            with self.subTest(round=round_index):
                outcomes.append(self._round(round_index))
        self.assertEqual(set(outcomes), {"cancelled", "completed"}, "both race winners must be exercised")

    def _round(self, round_index):
        store, jobs = SessionStore(), AnalysisJobs()
        server = FakeQwenServer(FakeQwenServer.HOLD)
        self.addCleanup(server.close)
        provider = HttpQwenProvider(base_url=server.base_url, api_key=API_KEY, model="qwen", timeout_seconds=30)
        client = TestClient(create_app(store, lambda: provider, ScriptedSandbox(), jobs))
        session_id = client.post("/api/sessions", json={"scope": "range"}).json()["sessionId"]
        analysis_id = f"analysis_race_{round_index:02d}"
        body = {"analysisId": analysis_id, "scope": "range", "question": QUESTION, "sessionId": session_id, "artifact": artifact("P&L", "P&L!A1:C3", 3, 3)}
        start = client.post("/api/analysis", json=body)
        self.assertTrue(server.held.wait(WAIT))
        cancel_response = {}
        go = threading.Barrier(2)

        # Even rounds give the cancel a head start, odd rounds give the reply one (up to ~0.1 s),
        # so both winners occur; within a round the order is still decided by the race itself.
        def cancel():
            go.wait(WAIT)
            if round_index % 2:
                time.sleep(0.01 * round_index)
            cancel_response.update(client.post(f"/api/analyses/{analysis_id}/cancel").json())

        def reply():
            go.wait(WAIT)
            if not round_index % 2:
                time.sleep(0.001 * round_index)
            server.release(chat_reply(ANSWER))

        threads = [threading.Thread(target=cancel), threading.Thread(target=reply)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(WAIT)
        final = follow(client, start)
        time.sleep(0.2)
        later = client.get(f"/api/analyses/{analysis_id}").json()
        history = store.snapshot(session_id).history
        # The cancel response already tells the add-in the outcome; it never changes afterwards.
        self.assertEqual(later["stage"], final["status"], "no late completed result after cancel")
        if final["status"] == "cancelled":
            self.assertEqual(cancel_response["stage"], "cancelled")
            self.assertNotIn("narrative", later["result"])
            self.assertEqual(history, (), "a cancelled answer is never recorded in the dialog")
        else:
            self.assertEqual(final["status"], "completed")
            self.assertIn(cancel_response["stage"], ("finalizing", "completed"), "a refused cancel never reports cancelled")
            self.assertEqual(len(history), 1)
        self.assertEqual(len(server.requests), 1)
        return final["status"]


if __name__ == "__main__":
    unittest.main()
