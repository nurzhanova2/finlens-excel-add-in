"""Run the sandbox security suite and fail (instead of skip) when Docker or the image is missing.

Usage from the repository root: `npm run test:sandbox` (or `python api/tests/run_sandbox_suite.py`).
"""
import os
import sys
import unittest
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
os.environ["FINLENS_REQUIRE_SANDBOX_TESTS"] = "1"

MODULES = ["tests.test_sandbox_docker", "tests.test_sandbox_runner", "tests.test_tool_calls", "tests.test_agent", "tests.test_lifecycle", "tests.test_qwen_cancellation"]

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromNames(MODULES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
