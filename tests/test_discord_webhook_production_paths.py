from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from orchestrator.api.main import create_app
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.storage.models import Tenant
from tests.production_path_support import (
    DeferredTaskHarness,
    clear_runtime_environment,
    configure_runtime_environment,
    seed_core_runtime_state,
    session_factory_for,
)

pytestmark = pytest.mark.production_path


class DiscordWebhookProductionPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url, _ = configure_runtime_environment(
            temp_dir=self.temp_dir,
            database_name="discord_webhook_production.db",
        )
        self.session_factory = session_factory_for(self.database_url)
        seed_core_runtime_state(self.session_factory)
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()
        clear_runtime_environment()

    def test_webhook_ask_runs_real_deferred_command_path(self) -> None:
        harness = DeferredTaskHarness()

        with (
            patch("orchestrator.api.routes.webhook_discord.asyncio.create_task", new=harness.create_task),
            patch(
                "orchestrator.api.discord.ingress.ask_runtime._search_jira_issues_for_tenant",
                return_value=[],
            ),
            patch("orchestrator.api.discord.ingress.ask_runtime.build_codex_runtime"),
            patch("orchestrator.api.discord.ingress.ask_runtime.answer_board_question_with_codex", return_value="Board answer"),
        ):
            response = self.client.post(
                "/discord/webhook/example",
                json={
                    "user_id": "u-1",
                    "channel_id": "discord-channel-1",
                    "command": "!ask what is on the board?",
                },
            )
            harness.wait()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        self.assertTrue(response.json()["deferred"])
        with self.session_factory() as session:
            tenant = session.get(Tenant, "example")
            assert tenant is not None
            ask_history = list((tenant.discord_config or {}).get("ask_history") or [])
        self.assertEqual(len(ask_history), 1)
        self.assertEqual(ask_history[0]["question"], "what is on the board?")
        self.assertEqual(ask_history[0]["answer"], "Board answer")

    def test_webhook_ask_surfaces_real_deferred_failure(self) -> None:
        harness = DeferredTaskHarness()

        with (
            patch("orchestrator.api.routes.webhook_discord.asyncio.create_task", new=harness.create_task),
            patch(
                "orchestrator.api.discord.ingress.ask_runtime._search_jira_issues_for_tenant",
                return_value=[],
            ),
            patch("orchestrator.api.discord.ingress.ask_runtime.build_codex_runtime"),
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.answer_board_question_with_codex",
                side_effect=CodexRuntimeError(
                    "Codex CLI command failed (exit=1): You've hit your usage limit for GPT-5.3-Codex-Spark."
                ),
            ),
        ):
            response = self.client.post(
                "/discord/webhook/example",
                json={
                    "user_id": "u-1",
                    "channel_id": "discord-channel-1",
                    "command": "!ask what is on the board?",
                },
            )
            with self.assertRaises(HTTPException):  # type: ignore[name-defined]
                harness.wait()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
