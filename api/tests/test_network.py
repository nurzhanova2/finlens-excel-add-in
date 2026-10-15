import unittest

from fastapi.testclient import TestClient

from app.adapters.qwen import FakeQwenProvider
from app.application.sessions import SessionStore
from app.main import create_app


class NetworkBoundaryTests(unittest.TestCase):
    """The task pane reaches the API through the same-origin dev proxy, so the API grants no CORS."""

    def setUp(self):
        self.client = TestClient(create_app(SessionStore(), FakeQwenProvider))

    def test_cross_origin_requests_get_no_cors_grant(self):
        for origin in ("https://evil.example", "https://localhost:5173", "null"):
            response = self.client.post("/api/sessions", json={"scope": "range"}, headers={"Origin": origin})
            self.assertEqual(response.status_code, 201)
            self.assertNotIn("access-control-allow-origin", response.headers)

    def test_preflight_is_not_answered(self):
        response = self.client.options("/api/analysis", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
        self.assertEqual(response.status_code, 405)
        self.assertNotIn("access-control-allow-origin", response.headers)


if __name__ == "__main__":
    unittest.main()
