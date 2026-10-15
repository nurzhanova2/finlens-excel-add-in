"""Story 2.1/2.2: the API accepts only a strict run_analysis call and rejects unsafe code before any job."""
import json
import unittest

from app.adapters.qwen import RawToolCall
from app.application.agent import validate_tool_call
from app.domain.code_policy import check_code, check_text
from app.domain.computation import MAX_CODE_CHARS, MAX_CODE_LINES, SafeDiagnostic, run_analysis_tool_schema
from app.domain.contracts import TabularArtifact
from tests.support import artifact

ARTIFACTS = {
    "artifact_1": TabularArtifact.model_validate(artifact("P&L", "'P&L'!A1:C3", 3, 3)),
    "artifact_2": TabularArtifact.model_validate(artifact("Debt", "Debt!B2:C4", 3, 2)),
}
GOOD_CODE = 'data = table("artifact_1")\nshow_table(data.describe(), "Статистика")\n'


def call(arguments, name="run_analysis"):
    text = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return (RawToolCall(call_id="call_1", name=name, arguments=text),)


def args(**overrides):
    base = {"code": GOOD_CODE, "inputs": ["artifact_1"], "method": "Описательная статистика по столбцам."}
    base.update(overrides)
    return base


class ToolSchemaTests(unittest.TestCase):
    def assertRejected(self, calls, fragment=""):
        result = validate_tool_call(calls, ARTIFACTS)
        self.assertIsInstance(result, SafeDiagnostic)
        self.assertEqual(result.code, "TOOL_CALL_REJECTED")
        self.assertIn(fragment, result.message)
        return result

    def test_valid_call_maps_only_referenced_artifacts(self):
        accepted = validate_tool_call(call(args(inputs=["artifact_2"])), ARTIFACTS)
        self.assertNotIsInstance(accepted, SafeDiagnostic)
        self.assertEqual([(item.ref, item.artifact.sheetName) for item in accepted.inputs], [("artifact_2", "Debt")])

    def test_tool_schema_is_strict_and_published_to_the_model(self):
        schema = run_analysis_tool_schema()
        self.assertEqual(schema["function"]["name"], "run_analysis")
        parameters = schema["function"]["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(set(parameters["required"]), {"code", "inputs", "method"})

    def test_unknown_tool_and_multiple_calls_are_rejected(self):
        self.assertRejected(call(args(), name="run_shell"), "only available tool is run_analysis")
        self.assertRejected(call(args()) + call(args()), "exactly one")

    def test_invalid_json_and_missing_fields_never_start_a_job(self):
        self.assertRejected(call("{not json"), "schema")
        self.assertRejected(call({"code": GOOD_CODE, "inputs": ["artifact_1"]}), "method")
        self.assertRejected(call({"inputs": ["artifact_1"], "method": "m"}), "code")
        self.assertRejected(call(args(inputs=[])), "inputs")
        self.assertRejected(call(args(code=123)), "code")

    def test_command_url_path_and_credential_fields_are_forbidden(self):
        for field, value in (("command", "bash -c id"), ("shell", "sh"), ("url", "https://evil.example"), ("path", "/etc/passwd"), ("env", {"FINLENS_QWEN_API_KEY": "x"}), ("image", "alpine"), ("network", "host"), ("timeout", 9999), ("apiKey", "x")):
            with self.subTest(field=field):
                self.assertRejected(call(args(**{field: value})), field)

    def test_only_artifacts_of_the_current_analysis_can_be_referenced(self):
        self.assertRejected(call(args(inputs=["artifact_3"])), "not part of this analysis")
        self.assertRejected(call(args(inputs=["P&L!A1:C3"])), "inputs")
        self.assertRejected(call(args(inputs=["../session/other"])), "inputs")
        self.assertRejected(call(args(inputs=["artifact_1", "artifact_1"])), "inputs")

    def test_size_limits_are_enforced(self):
        self.assertRejected(call(args(code="x = 1\n" * (MAX_CODE_LINES + 1))), "code")
        self.assertRejected(call(args(code="#" * (MAX_CODE_CHARS + 1))), "code")
        self.assertRejected(call(args(method="m" * 2_001)), "method")
        self.assertRejected(call(args(inputs=[f"artifact_{i}" for i in range(1, 7)])), "inputs")

    def test_method_with_url_or_secret_is_rejected(self):
        self.assertRejected(call(args(method="See http://intranet/report")), "URL")
        self.assertRejected(call(args(method="use FINLENS_QWEN_API_KEY")), "credentials")


class CodePolicyTests(unittest.TestCase):
    def assertBlocked(self, code, fragment=""):
        violation = check_code(code)
        self.assertIsNotNone(violation, code)
        self.assertIn(fragment, violation.reason)

    def test_allowed_analysis_code_passes(self):
        for code in (
            GOOD_CODE,
            "import pandas as pd\nimport numpy as np\nimport matplotlib.pyplot as plt\nfrom collections import Counter\nimport math, statistics, datetime\n"
            "df = frame('artifact_1')\ngrowth = df.pct_change(axis=1)\nfig, ax = plt.subplots()\nax.plot([1, 2])\n"
            "show_chart(fig, title='Рост', metric='Выручка', period='2024–2025', insight=f'Рост {growth.max().max():.1%}')\nprint('Revenue / EBITDA', 'yyyy/mm')\n",
        ):
            with self.subTest(code=code[:30]):
                self.assertIsNone(check_code(code))

    def test_subprocess_shell_and_os_access_is_blocked(self):
        self.assertBlocked("import os\nos.system('id')", "import of 'os'")
        self.assertBlocked("import subprocess\nsubprocess.run(['sh'])", "import of 'subprocess'")
        self.assertBlocked("from os import system", "import from 'os'")
        self.assertBlocked("pd.io.common.os.system('id')", "attribute")
        self.assertBlocked("np.ctypeslib", "attribute")
        self.assertBlocked("from numpy import ctypeslib", "numpy.ctypeslib")

    def test_network_access_is_blocked(self):
        self.assertBlocked("import socket\nsocket.create_connection(('10.0.0.1', 80))", "socket")
        self.assertBlocked("import urllib.request", "urllib")
        self.assertBlocked("pd.read_html('https://evil.example')", "")
        self.assertBlocked("x = 'ftp://data.example/file'", "URL")

    def test_filesystem_access_is_blocked(self):
        self.assertBlocked("open('/etc/passwd').read()", "open")
        self.assertBlocked("df.to_csv('out.csv')", "to_csv")
        self.assertBlocked("pd.read_csv('data.csv')", "read_csv")
        self.assertBlocked("np.load('x.npy')", "load")
        self.assertBlocked("fig.savefig('chart.png')", "savefig")
        self.assertBlocked("path = '/proc/self/environ'", "file paths")
        self.assertBlocked("path = 'C:\\\\Users\\\\analyst\\\\book.xlsx'", "file paths")
        self.assertBlocked("path = '../secrets'", "file paths")

    def test_secrets_and_interpreter_escapes_are_blocked(self):
        self.assertBlocked("key = 'FINLENS_QWEN_API_KEY'", "credentials")
        self.assertBlocked("h = 'Bearer abc'", "credentials")
        self.assertBlocked("eval('1+1')", "eval")
        self.assertBlocked("exec('x=1')", "exec")
        self.assertBlocked("__import__('os')", "__import__")
        self.assertBlocked("getattr(df, 'to_csv')", "getattr")
        self.assertBlocked("().__class__.__base__.__subclasses__()", "dunder")
        self.assertBlocked("import importlib", "importlib")
        self.assertBlocked("from . import x", "import from")
        self.assertBlocked("from numpy import *", "numpy.*")

    def test_excel_and_office_are_not_reachable(self):
        self.assertBlocked("Excel.run(lambda ctx: None)", "Excel")
        self.assertBlocked("Office.context", "Office")
        self.assertBlocked("x.Excel", "Excel")

    def test_syntax_errors_are_reported_with_a_line(self):
        violation = check_code("x = (\n")
        self.assertIn("syntax error", violation.reason)

    def test_free_text_check(self):
        self.assertIsNone(check_text("Сравнение 2025 к 2024 году", "method"))
        self.assertIsNotNone(check_text("www.example.com", "method"))


if __name__ == "__main__":
    unittest.main()
