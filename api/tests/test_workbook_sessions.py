import json
import logging
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.adapters.qwen import AnswerContext, FakeQwenProvider, HttpQwenProvider, SelectionContext, build_selection_messages
from app.application.analyze import AnalysisFailure
from app.application.sessions import DEFAULT_SESSION_TTL_SECONDS, SessionStore, load_session_ttl_seconds
from app.application.workbook import validate_selections
from app.domain.contracts import MAX_ARTIFACT_CELLS, MAX_MAP_SHEETS, MAX_WORKBOOK_CELLS, RangeSelection, WorkbookMap
from app.main import create_app
from tests.support import FakeQwenServer, chat_reply, follow

SECRET_CELL = "CONFIDENTIAL-CELL-42"


def sheet(name, address, rows, columns, index=0, tables=None):
    return {"sheetName": name, "index": index, "visibility": "Visible", "usedRangeAddress": address, "rowCount": rows, "columnCount": columns, "tables": tables or []}


def workbook_map():
    return {"sheets": [
        sheet("P&L", "'P&L'!A1:C3", 3, 3, 0, [{"name": "PnlTable", "address": "'P&L'!A1:C3"}]),
        sheet("Debt", "Debt!B2:C4", 3, 2, 1),
        {"sheetName": "Notes", "index": 2, "visibility": "Hidden", "usedRangeAddress": None, "rowCount": 0, "columnCount": 0, "tables": []},
    ]}


def artifact(sheet_name, address, rows, columns, value=1):
    return {"sheetName": sheet_name, "address": address, "values": [[value] * columns for _ in range(rows)], "formulas": [[""] * columns for _ in range(rows)], "numberFormats": [["General"] * columns for _ in range(rows)]}


class RecordingProvider(FakeQwenProvider):
    def __init__(self, selections=None):
        self.answers: list[AnswerContext] = []
        self.selection_calls: list[SelectionContext] = []
        self._selections = selections

    def next_turn(self, context, exchanges, tools_enabled, cancel=None):
        if not exchanges:
            self.answers.append(context)
        return super().next_turn(context, exchanges, tools_enabled, cancel)

    def select_ranges(self, context, cancel=None):
        self.selection_calls.append(context)
        if self._selections is not None:
            return [RangeSelection(**selection) for selection in self._selections]
        return super().select_ranges(context)


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


class WorkbookSessionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = SessionStore(ttl_seconds=120, clock=self.clock)
        self.provider = RecordingProvider()
        self.client = TestClient(create_app(self.store, lambda: self.provider))

    def session(self, scope="workbook"):
        response = self.client.post("/api/sessions", json={"scope": scope})
        self.assertEqual(response.status_code, 201)
        return response.json()["sessionId"]

    def plan(self, session_id, analysis_id="analysis_0001", question="Какие ключевые допущения модели?", include_map=True):
        body = {"analysisId": analysis_id, "question": question}
        if include_map:
            body["workbookMap"] = workbook_map()
        return self.client.post(f"/api/sessions/{session_id}/workbook/plan", json=body)

    def plan_result(self, session_id, **kwargs):
        return follow(self.client, self.plan(session_id, **kwargs), until_planned=True)

    def analyze(self, session_id, analysis_id, artifacts):
        return follow(self.client, self.client.post(f"/api/sessions/{session_id}/workbook/analysis", json={"analysisId": analysis_id, "artifacts": artifacts}))

    def read_planned(self, plan):
        # Simulates the add-in reading exactly the planned ranges from Excel.
        artifacts = []
        for selection in plan["selections"]:
            from app.domain.addresses import parse_a1
            bounds = parse_a1(selection["address"])
            artifacts.append(artifact(selection["sheetName"], f"{selection['sheetName']}!{selection['address']}", bounds.rows, bounds.columns, SECRET_CELL))
        return artifacts

    def test_session_id_is_opaque_and_ttl_is_reported(self):
        response = self.client.post("/api/sessions", json={"scope": "workbook"}).json()
        self.assertGreaterEqual(len(response["sessionId"]), 32)
        self.assertNotIn("P&L", response["sessionId"])
        self.assertEqual(response["ttlSeconds"], 120)

    def test_two_phase_workbook_flow_sends_only_planned_bounded_artifacts(self):
        session_id = self.session()
        plan = self.plan_result(session_id)
        self.assertEqual(plan["status"], "planned")
        self.assertEqual([(s["sheetName"], s["address"]) for s in plan["selections"]], [("P&L", "A1:C3"), ("Debt", "B2:C4")])
        self.assertEqual(plan["sheetsInWorkbook"], 3)
        self.assertEqual(plan["limits"], {"maxArtifacts": 5, "maxCellsPerArtifact": MAX_ARTIFACT_CELLS, "maxTotalCells": MAX_WORKBOOK_CELLS})
        # Phase 1 sees only metadata: no cell payload exists in the selection prompt.
        prompt = build_selection_messages(self.provider.selection_calls[0])[1]["content"]
        self.assertNotIn("values", prompt)
        self.assertNotIn("formulas", prompt)

        result = self.analyze(session_id, "analysis_0001", self.read_planned(plan))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["sources"], [{"sheetName": "P&L", "address": "P&L!A1:C3"}, {"sheetName": "Debt", "address": "Debt!B2:C4"}])
        self.assertEqual(result["coverage"], {"sheetsInWorkbook": 3, "sheetsWithData": 2, "sheetsUsed": ["P&L", "Debt"], "complete": True})
        sent = self.provider.answers[0]
        self.assertEqual([a.address for a in sent.artifacts], ["P&L!A1:C3", "Debt!B2:C4"])
        self.assertEqual(sent.question, "Какие ключевые допущения модели?")

    def test_partial_workbook_coverage_is_explicit(self):
        self.provider = RecordingProvider(selections=[{"sheetName": "P&L", "address": "A1:C2", "reason": "Заголовки"}])
        session_id = self.session()
        plan = self.plan_result(session_id)
        result = self.analyze(session_id, "analysis_0001", self.read_planned(plan))
        # Only part of one sheet out of two sheets with data: never presented as the whole workbook.
        self.assertEqual(result["coverage"], {"sheetsInWorkbook": 3, "sheetsWithData": 2, "sheetsUsed": ["P&L"], "complete": False})
        self.assertEqual(result["sources"], [{"sheetName": "P&L", "address": "P&L!A1:C2"}])

    def test_unplanned_or_extra_artifacts_never_reach_qwen(self):
        session_id = self.session()
        plan = self.plan_result(session_id)
        artifacts = self.read_planned(plan) + [artifact("Notes", "Notes!A1:A2", 2, 1)]
        response = self.client.post(f"/api/sessions/{session_id}/workbook/analysis", json={"analysisId": "analysis_0001", "artifacts": artifacts})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["result"]["error"]["code"], "ARTIFACTS_MISMATCH")
        self.assertEqual(self.provider.answers, [])
        # The plan is single-use and the analysis is finished: a retry with the same analysisId never runs.
        retry = self.client.post(f"/api/sessions/{session_id}/workbook/analysis", json={"analysisId": "analysis_0001", "artifacts": self.read_planned(plan)})
        self.assertEqual(retry.json()["result"]["error"]["code"], "ARTIFACTS_MISMATCH")
        self.assertEqual(self.provider.answers, [])
        fresh = self.client.post(f"/api/sessions/{session_id}/workbook/analysis", json={"analysisId": "analysis_0009", "artifacts": self.read_planned(plan)})
        self.assertEqual(fresh.json()["result"]["error"]["code"], "PLAN_NOT_FOUND")

    def test_follow_up_reuses_session_map_and_history_without_resending_map(self):
        session_id = self.session()
        plan = self.plan_result(session_id)
        self.analyze(session_id, "analysis_0001", self.read_planned(plan))

        follow_up = self.plan_result(session_id, analysis_id="analysis_0002", question="А какие риски у долга?", include_map=False)
        self.assertEqual(follow_up["status"], "planned")
        self.assertEqual(len(self.provider.selection_calls[1].history), 1)
        self.assertEqual(self.provider.selection_calls[1].workbook_map, WorkbookMap.model_validate(workbook_map()))
        result = self.analyze(session_id, "analysis_0002", self.read_planned(follow_up))
        self.assertIn("1 предыдущих", result["narrative"]["evidence"][1])
        self.assertEqual(self.provider.answers[1].history[0].question, "Какие ключевые допущения модели?")

    def test_follow_up_for_range_scope_uses_session_history(self):
        session_id = self.session("range")
        body = {"analysisId": "analysis_0001", "sessionId": session_id, "scope": "range", "question": "Что в диапазоне?", "artifact": artifact("P&L", "P&L!A1:B1", 1, 2)}
        follow(self.client, self.client.post("/api/analysis", json=body))
        body["analysisId"], body["question"] = "analysis_0002", "А почему?"
        follow(self.client, self.client.post("/api/analysis", json=body))
        self.assertEqual(len(self.provider.answers[1].history), 1)

    def test_scope_change_requires_a_clean_session(self):
        range_session = self.session("range")
        response = self.plan(range_session)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["result"]["error"]["code"], "SESSION_SCOPE_MISMATCH")
        body = {"analysisId": "analysis_0002", "sessionId": self.session("workbook"), "scope": "sheet", "question": "Что?", "artifact": artifact("P&L", "P&L!A1", 1, 1)}
        mismatch = self.client.post("/api/analysis", json=body)
        self.assertEqual((mismatch.status_code, mismatch.json()["result"]["error"]["code"]), (409, "SESSION_SCOPE_MISMATCH"))
        self.assertEqual(self.provider.answers, [])

    def test_close_endpoint_and_ttl_remove_all_session_data(self):
        session_id = self.session()
        self.plan(session_id)
        self.assertEqual(self.client.delete(f"/api/sessions/{session_id}").status_code, 204)
        self.assertEqual(len(self.store), 0)
        self.assertEqual(self.plan(session_id, analysis_id="analysis_0002").status_code, 404)

        beacon_session = self.session()
        self.assertEqual(self.client.post(f"/api/sessions/{beacon_session}/close").status_code, 204)
        self.assertEqual(len(self.store), 0)

        expiring = self.session()
        self.plan(expiring, analysis_id="analysis_0003")
        self.clock.now += 121
        response = self.plan(expiring, analysis_id="analysis_0004", include_map=False)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["result"]["error"]["code"], "SESSION_NOT_FOUND")
        self.assertEqual(len(self.store), 0)

    def test_purge_expired_clears_idle_sessions_and_activity_extends_ttl(self):
        active, idle = self.store.create("workbook"), self.store.create("workbook")
        self.clock.now += 100
        self.store.snapshot(active)
        self.clock.now += 30
        self.assertEqual(self.store.purge_expired(), 1)
        self.store.snapshot(active)

    def test_too_large_workbook_and_payloads_are_rejected_with_next_action(self):
        too_many = {"sheets": [sheet(f"S{i}", f"S{i}!A1", 1, 1, i) for i in range(MAX_MAP_SHEETS + 1)]}
        response = self.client.post(f"/api/sessions/{self.session()}/workbook/plan", json={"analysisId": "analysis_0001", "question": "Что?", "workbookMap": too_many})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "REQUEST_INVALID")
        self.assertTrue(response.json()["error"]["nextAction"])

        huge = [artifact("P&L", f"P&L!A1:A{MAX_ARTIFACT_CELLS}", MAX_ARTIFACT_CELLS, 1) for _ in range(3)]
        response = self.client.post(f"/api/sessions/{self.session()}/workbook/analysis", json={"analysisId": "analysis_0001", "artifacts": huge})
        self.assertEqual(response.status_code, 422)

    def test_greedy_model_selection_is_explained_not_sent(self):
        big_map = {"sheets": [sheet("Data", "Data!A1:Z5000", 5000, 26)]}
        self.provider = RecordingProvider(selections=[{"sheetName": "Data", "address": "A1:Z5000"}])
        session_id = self.session()
        result = follow(self.client, self.client.post(f"/api/sessions/{session_id}/workbook/plan", json={"analysisId": "analysis_0001", "question": "Что?", "workbookMap": big_map}), until_planned=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "WORKBOOK_SCOPE_TOO_LARGE")
        self.assertIn("Лист", result["error"]["nextAction"])

    def test_logs_contain_only_operational_metadata(self):
        session_id = self.session()
        with self.assertLogs("app", level=logging.INFO) as logs:
            plan = self.plan_result(session_id, question="Секретный вопрос о марже")
            self.analyze(session_id, "analysis_0001", self.read_planned(plan))
        text = "\n".join(logs.output)
        self.assertIn("analysisId=analysis_0001", text)
        for forbidden in (SECRET_CELL, "Секретный", session_id, "P&L"):
            self.assertNotIn(forbidden, text)


class SelectionValidationTests(unittest.TestCase):
    def setUp(self):
        self.map = WorkbookMap.model_validate(workbook_map())

    def test_selection_is_clipped_to_used_range_and_unknown_sheets_dropped(self):
        selections = validate_selections([
            RangeSelection(sheetName="P&L", address="'P&L'!A1:Z99"),
            RangeSelection(sheetName="Missing", address="A1:B2"),
            RangeSelection(sheetName="Notes", address="A1:B2"),
            RangeSelection(sheetName="Debt", address="Other!B2:C4"),
        ], self.map)
        self.assertEqual([(s.sheetName, s.address) for s in selections], [("P&L", "A1:C3")])

    def test_empty_or_oversized_selection_fails_honestly(self):
        with self.assertRaises(AnalysisFailure) as empty:
            validate_selections([RangeSelection(sheetName="Missing", address="A1")], self.map)
        self.assertEqual(empty.exception.code, "NO_RELEVANT_DATA")
        wide = WorkbookMap.model_validate({"sheets": [sheet(f"S{i}", f"S{i}!A1:A9000", 9000, 1, i) for i in range(3)]})
        with self.assertRaises(AnalysisFailure) as total:
            validate_selections([RangeSelection(sheetName=f"S{i}", address="A1:A9000") for i in range(3)], wide)
        self.assertEqual(total.exception.code, "WORKBOOK_SCOPE_TOO_LARGE")

    def test_workbook_map_is_metadata_only_and_consistent(self):
        leaking = workbook_map()
        leaking["sheets"][0]["values"] = [[SECRET_CELL]]
        with self.assertRaises(ValidationError):
            WorkbookMap.model_validate(leaking)
        inconsistent = workbook_map()
        inconsistent["sheets"][0]["rowCount"] = 99
        with self.assertRaises(ValidationError):
            WorkbookMap.model_validate(inconsistent)


class ConfigurationAndPromptTests(unittest.TestCase):
    def test_session_ttl_is_configurable_with_safe_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(load_session_ttl_seconds(), DEFAULT_SESSION_TTL_SECONDS)
        with patch.dict("os.environ", {"FINLENS_SESSION_TTL_SECONDS": "600"}, clear=True):
            self.assertEqual(load_session_ttl_seconds(), 600)
        with patch.dict("os.environ", {"FINLENS_SESSION_TTL_SECONDS": "forever"}, clear=True):
            self.assertEqual(load_session_ttl_seconds(), DEFAULT_SESSION_TTL_SECONDS)

    def test_http_provider_sends_structured_prompt_with_history_and_validates_output(self):
        selection_json = json.dumps({"selections": [{"sheetName": "P&L", "address": "A1:C3", "reason": "Выручка"}]})
        server = FakeQwenServer(chat_reply(selection_json))
        self.addCleanup(server.close)
        provider = HttpQwenProvider(base_url=server.base_url, api_key="server-secret", model="qwen")
        context = SelectionContext(analysis_id="analysis_0001", question="Что?", workbook_map=WorkbookMap.model_validate(workbook_map()))
        selections = provider.select_ranges(context)
        self.assertEqual(selections[0].address, "A1:C3")
        messages = server.requests[0]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("at most 5 ranges", messages[0]["content"])
        self.assertNotIn("server-secret", json.dumps(messages))
        self.assertEqual(server.auth, ["Bearer server-secret"])

if __name__ == "__main__":
    unittest.main()
