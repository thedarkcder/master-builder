from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from orchestrator.api.main import create_app
from orchestrator.core.config import get_settings
from orchestrator.core.worker.webhook_job_service import process_next_webhook_job
from orchestrator.storage.models import Tenant, WebhookJob
from tests.production_path_support import (
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

    def _process_one_job(self):
        with self.session_factory() as session:
            return process_next_webhook_job(
                session=session,
                settings=get_settings(),
                owner_id="worker:test",
            )

    def test_webhook_ask_runs_real_queued_command_path(self) -> None:
        def _fake_execute(*, tenant_id, payload, session, **_kwargs):
            tenant = session.get(Tenant, tenant_id)
            assert tenant is not None
            ask_history = list((tenant.discord_config or {}).get("ask_history") or [])
            ask_history.append(
                {
                    "question": "what is on the board?",
                    "answer": "Board answer",
                    "issue_key": None,
                    "status": None,
                }
            )
            tenant.discord_config = dict(tenant.discord_config or {}, ask_history=ask_history)
            session.flush()
            return object()

        with patch(
            "orchestrator.core.worker.webhook_job_service.execute_tenant_discord_ingress_command",
            side_effect=_fake_execute,
        ):
            response = self.client.post(
                "/discord/webhook/example",
                json={
                    "user_id": "u-1",
                    "channel_id": "discord-channel-1",
                    "command": "!ask what is on the board?",
                },
            )
            processed = self._process_one_job()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        self.assertTrue(response.json()["deferred"])
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        with self.session_factory() as session:
            tenant = session.get(Tenant, "example")
            assert tenant is not None
            ask_history = list((tenant.discord_config or {}).get("ask_history") or [])
        self.assertEqual(len(ask_history), 1)
        self.assertEqual(ask_history[0]["question"], "what is on the board?")
        self.assertEqual(ask_history[0]["answer"], "Board answer")

    def test_webhook_ask_surfaces_real_queued_failure(self) -> None:
        with patch(
            "orchestrator.core.worker.webhook_job_service.execute_tenant_discord_ingress_command",
            side_effect=HTTPException(
                status_code=503,
                detail="Codex board assistant is unavailable: You've hit your usage limit for GPT-5.3-Codex-Spark.",
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
            processed = self._process_one_job()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "failed")
        with self.session_factory() as session:
            job = session.get(WebhookJob, processed.job_id)
            assert job is not None
            self.assertEqual(job.status, "failed")
            self.assertIn("Codex board assistant is unavailable", str(job.last_error))
