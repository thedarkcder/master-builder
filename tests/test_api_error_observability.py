import unittest
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from orchestrator.api.main import create_app

pytestmark = pytest.mark.contract


class ApiErrorObservabilityTests(unittest.TestCase):
    def test_create_app_registers_discord_executor_once_on_startup(self) -> None:
        with (
            patch("orchestrator.api.main.run_migrations"),
            patch("orchestrator.api.main.ensure_execution_snapshot_startup_bootstrap"),
            patch("orchestrator.api.main.register_discord_command_executor") as register_mock,
            patch("orchestrator.api.main.sync_discord_guild_commands"),
        ):
            app = create_app()
            with TestClient(app, raise_server_exceptions=False):
                pass

        register_mock.assert_called_once()

    def test_create_app_does_not_start_knowledge_jira_sync_runtime(self) -> None:
        with (
            patch("orchestrator.api.main.run_migrations"),
            patch("orchestrator.api.main.ensure_execution_snapshot_startup_bootstrap"),
            patch("orchestrator.api.main.register_discord_command_executor"),
            patch("orchestrator.api.main.sync_discord_guild_commands"),
            patch("orchestrator.core.knowledge.jira_sync_runtime.run_knowledge_jira_sync") as sync_runtime_mock,
        ):
            app = create_app()
            with TestClient(app, raise_server_exceptions=False):
                pass

        sync_runtime_mock.assert_not_called()

    def test_unhandled_exception_logs_and_returns_error_ref(self) -> None:
        app = create_app()

        @app.get("/_test/unhandled")
        def _unhandled() -> dict:
            raise RuntimeError("boom")

        with (
            patch("orchestrator.api.main.run_migrations"),
            patch("orchestrator.api.main.ensure_execution_snapshot_startup_bootstrap"),
            patch("orchestrator.api.main.register_discord_command_executor"),
            patch("orchestrator.api.main.sync_discord_guild_commands"),
            patch("orchestrator.api.main.logger.exception") as exception_log,
            patch("orchestrator.api.main.emit_hard_error") as hard_error,
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get("/_test/unhandled")

        self.assertEqual(response.status_code, 500)
        self.assertIn("Internal server error. Ref:", response.json()["detail"])
        exception_log.assert_called_once()
        hard_error.assert_called_once()

    def test_http_exception_500_is_logged(self) -> None:
        app = create_app()

        @app.get("/_test/http-500")
        def _http_500() -> dict:
            raise HTTPException(status_code=500, detail="forced 500")

        with (
            patch("orchestrator.api.main.run_migrations"),
            patch("orchestrator.api.main.ensure_execution_snapshot_startup_bootstrap"),
            patch("orchestrator.api.main.register_discord_command_executor"),
            patch("orchestrator.api.main.sync_discord_guild_commands"),
            patch("orchestrator.api.main.logger.error") as error_log,
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get("/_test/http-500")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "forced 500")
        self.assertGreaterEqual(error_log.call_count, 1)

    def test_http_exception_headers_are_preserved(self) -> None:
        app = create_app()

        @app.get("/_test/http-401")
        def _http_401() -> dict:
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

        with (
            patch("orchestrator.api.main.run_migrations"),
            patch("orchestrator.api.main.ensure_execution_snapshot_startup_bootstrap"),
            patch("orchestrator.api.main.register_discord_command_executor"),
            patch("orchestrator.api.main.sync_discord_guild_commands"),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get("/_test/http-401")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Unauthorized")
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
    def test_method_not_allowed_is_request_logged(self) -> None:
        app = create_app()

        with (
            patch("orchestrator.api.main.run_migrations"),
            patch("orchestrator.api.main.ensure_execution_snapshot_startup_bootstrap"),
            patch("orchestrator.api.main.register_discord_command_executor"),
            patch("orchestrator.api.main.sync_discord_guild_commands"),
            patch("orchestrator.api.main.logger.info") as info_log,
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            response = client.get("/jira/webhook/example")

        self.assertEqual(response.status_code, 405)
        self.assertGreaterEqual(info_log.call_count, 1)
        self.assertTrue(response.headers.get("X-Request-Id"))
