import unittest

from fastapi.testclient import TestClient

from orchestrator.api.main import app


class HealthEndpointTests(unittest.TestCase):
    def test_health_endpoint_returns_ok(self) -> None:
        client = TestClient(app)

        response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
