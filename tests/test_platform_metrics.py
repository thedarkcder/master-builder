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
        os.environ["ORCHESTRATOR_ATLASSIAN_OAUTH_STATE_SECRET"] = "metrics-atlassian-state-secret"
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
        os.environ.pop("ORCHESTRATOR_ATLASSIAN_OAUTH_STATE_SECRET", None)
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

    def test_request_duration_histogram_buckets_count_each_request_once(self) -> None:
        platform_metrics.record_api_request(method="GET", route="/histogram", status_code=200, duration_seconds=0.2)
        payload = platform_metrics.render_prometheus()
        bucket_values: dict[str, int] = {}
        for line in payload.splitlines():
            if not line.startswith("master_builder_api_request_duration_seconds_bucket"):
                continue
            if 'method="GET"' not in line or 'route="/histogram"' not in line:
                continue
            labels, value = line.split("} ", maxsplit=1)
            bucket = labels.split('le="', maxsplit=1)[1].split('"', maxsplit=1)[0]
            bucket_values[bucket] = int(float(value))

        self.assertEqual(bucket_values["0.1"], 0)
        self.assertEqual(bucket_values["0.25"], 1)
        self.assertEqual(bucket_values["0.5"], 1)
        self.assertEqual(bucket_values["+Inf"], 1)

    def test_error_rate_uses_failed_request_count_not_error_counter_sum(self) -> None:
        platform_metrics.record_api_request(method="GET", route="/boom", status_code=500, duration_seconds=0.1)
        platform_metrics.record_api_exception(method="GET", route="/boom", error_type="runtimeerror")
        payload = platform_metrics.render_prometheus()
        error_rate_line = next(
            line for line in payload.splitlines() if line.startswith("master_builder_api_error_rate_ratio ")
        )
        error_rate = float(error_rate_line.split(" ", maxsplit=1)[1])
        self.assertEqual(error_rate, 1.0)

    def test_create_app_rejects_sqlite_without_test_opt_in(self) -> None:
        previous = os.environ.get("ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS")
        try:
            os.environ["ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS"] = "false"
            get_settings.cache_clear()
            with self.assertRaisesRegex(RuntimeError, "requires PostgreSQL"):
                create_app()
        finally:
            if previous is None:
                os.environ.pop("ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS", None)
            else:
                os.environ["ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS"] = previous
            get_settings.cache_clear()


if __name__ == "__main__":
    unittest.main()
