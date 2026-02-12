import os
import unittest
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

from orchestrator.api.main import create_app
from orchestrator.core.config import get_settings
from orchestrator.core.platform_metrics import platform_metrics, reset_platform_metrics_for_tests
from orchestrator.storage.db import reset_db_engine_cache


class PlatformMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        os.environ["ORCHESTRATOR_DATABASE_URL"] = f"sqlite:///{self.temp_dir.name}/metrics_test.db"
        os.environ["ORCHESTRATOR_ADMIN_USERNAME"] = "admin"
        os.environ["ORCHESTRATOR_ADMIN_PASSWORD"] = "secret"
        os.environ["ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET"] = "metrics-state-secret"
        os.environ["ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET"] = "metrics-jira-state-secret"
        os.environ["ORCHESTRATOR_GITHUB_APP_SLUG"] = "master-builder-app"
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
        get_settings.cache_clear()
        reset_db_engine_cache()
        reset_platform_metrics_for_tests()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        os.environ.pop("ORCHESTRATOR_ADMIN_USERNAME", None)
        os.environ.pop("ORCHESTRATOR_ADMIN_PASSWORD", None)
        os.environ.pop("ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET", None)
        os.environ.pop("ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET", None)
        os.environ.pop("ORCHESTRATOR_GITHUB_APP_SLUG", None)
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)
        get_settings.cache_clear()
        reset_db_engine_cache()
        reset_platform_metrics_for_tests()

    def test_metrics_endpoint_emits_request_histogram_and_runtime_gauges(self) -> None:
        app = create_app()
        client = TestClient(app)

        health = client.get("/health")
        self.assertEqual(health.status_code, 200)

        metrics = client.get("/metrics")
        self.assertEqual(metrics.status_code, 200)
        payload = metrics.text
        self.assertIn("master_builder_api_requests_total", payload)
        self.assertIn('route="/health"', payload)
        self.assertIn('status_class="2xx"', payload)
        self.assertIn("master_builder_api_request_duration_seconds_bucket", payload)
        self.assertIn("master_builder_process_uptime_seconds", payload)
        self.assertIn("master_builder_db_pool_saturation_ratio", payload)

    def test_metrics_endpoint_emits_5xx_error_counters(self) -> None:
        app = create_app()

        @app.get("/boom")
        def boom() -> dict:
            raise RuntimeError("boom")

        @app.get("/http-boom")
        def http_boom() -> dict:
            raise HTTPException(status_code=503, detail="failure")

        with TestClient(app, raise_server_exceptions=False) as client:
            boom = client.get("/boom")
            self.assertEqual(boom.status_code, 500)
            http_boom = client.get("/http-boom")
            self.assertEqual(http_boom.status_code, 503)

            metrics = client.get("/metrics")
            self.assertEqual(metrics.status_code, 200)
            payload = metrics.text
            self.assertIn("master_builder_api_request_errors_total", payload)
            self.assertIn('route="/boom"', payload)
            self.assertIn('error_type="runtimeerror"', payload)
            self.assertIn('route="/http-boom"', payload)
            self.assertIn('error_type="http_5xx"', payload)
            self.assertIn("master_builder_api_error_rate_ratio", payload)

    def test_worker_failure_counters_are_exported(self) -> None:
        platform_metrics.record_worker_failure(kind="dependency")
        platform_metrics.record_worker_failure(kind="crash")
        payload = platform_metrics.render_prometheus()
        self.assertIn('master_builder_worker_failures_total{kind="dependency"} 1', payload)
        self.assertIn('master_builder_worker_failures_total{kind="crash"} 1', payload)


if __name__ == "__main__":
    unittest.main()
