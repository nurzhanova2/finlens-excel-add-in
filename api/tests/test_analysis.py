import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.adapters.qwen import FakeQwenProvider, build_qwen_provider
from app.application.analyze import analyze
from app.domain.contracts import AnalysisRequest


def request(question: str = "Какие данные переданы?") -> AnalysisRequest:
    return AnalysisRequest.model_validate({
        "analysisId": "analysis_12345",
        "scope": "range",
        "question": question,
        "artifact": {"sheetName": "P&L", "address": "P&L!A1:B2", "values": [["Revenue", 100]], "formulas": [["", ""]], "numberFormats": [["General", "0"]]},
    })


class AnalysisContractTests(unittest.TestCase):
    def test_fake_provider_returns_structured_russian_answer_and_exact_source(self):
        result = analyze(request(), FakeQwenProvider())
        self.assertEqual(result.status, "completed")
        self.assertIn("Тестовый ответ", result.narrative.conclusion)
        self.assertEqual(result.sources[0].model_dump(), {"sheetName": "P&L", "address": "P&L!A1:B2"})
        self.assertLess(result.elapsedMs, 60_000)

    def test_request_rejects_workbook_and_unbounded_or_mismatched_artifact(self):
        invalid = request().model_dump()
        invalid["scope"] = "workbook"
        with self.assertRaises(ValidationError):
            AnalysisRequest.model_validate(invalid)
        invalid = request().model_dump()
        invalid["artifact"]["formulas"] = [[""]]
        with self.assertRaises(ValidationError):
            AnalysisRequest.model_validate(invalid)

    def test_fake_provider_is_explicit_test_mode(self):
        with patch.dict("os.environ", {"FINLENS_QWEN_PROVIDER": "fake"}, clear=True):
            self.assertIsInstance(build_qwen_provider(), FakeQwenProvider)


if __name__ == "__main__":
    unittest.main()
