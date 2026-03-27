from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from orchestrator.api.routes.webhook_discord import ingest_discord_webhook

pytestmark = pytest.mark.contract


class DiscordWebhookRouteTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, payload: dict, *, tenant=None, headers=None, **overrides):
        request = SimpleNamespace(headers=headers or {})
        session = MagicMock()
        session.get.return_value = tenant

        base = {
            "get_settings": MagicMock(return_value=SimpleNamespace(secrets_encryption_key="k")),
            "_read_json_payload": AsyncMock(return_value=(payload, b"{}")),
            "_extract_webhook_token": MagicMock(return_value="token"),
            "resolve_scoped_secret_ref": MagicMock(return_value="token"),
            "resolve_followup_context": MagicMock(return_value=None),
            "enqueue_webhook_job": MagicMock(
                return_value=SimpleNamespace(
                    created=True,
                    job=SimpleNamespace(job_id="job-1", dedupe_key="req-1"),
                )
            ),
            "notify_webhook_job_enqueued": MagicMock(),
        }
        base.update(overrides)

        with patch.multiple("orchestrator.api.routes.webhook_discord", **base):
            response = await ingest_discord_webhook("example", request=request, session=session)
        return response, session, base

    async def test_unknown_tenant(self) -> None:
        with self.assertRaises(HTTPException) as exc_ctx:
            await self._call(payload={}, tenant=None)
        self.assertEqual(exc_ctx.exception.status_code, 404)

    async def test_tenant_disabled(self) -> None:
        response, _, _ = await self._call(payload={}, tenant=SimpleNamespace(is_enabled=False, discord_config={}))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"tenant_disabled", response.body)

    async def test_secret_authentication_paths(self) -> None:
        tenant = SimpleNamespace(is_enabled=True, discord_config={"command_secret_ref": "ref"})

        with self.assertRaises(HTTPException) as exc_ctx:
            await self._call(payload={"user_id": "u", "command": "!ask"}, tenant=tenant, resolve_scoped_secret_ref=MagicMock(return_value=""))
        self.assertEqual(exc_ctx.exception.status_code, 500)

        with self.assertRaises(HTTPException) as exc_ctx:
            await self._call(payload={"user_id": "u", "command": "!ask"}, tenant=tenant, _extract_webhook_token=MagicMock(return_value=None))
        self.assertEqual(exc_ctx.exception.status_code, 401)

        with self.assertRaises(HTTPException) as exc_ctx:
            await self._call(payload={"user_id": "u", "command": "!ask"}, tenant=tenant, _extract_webhook_token=MagicMock(return_value="wrong"))
        self.assertEqual(exc_ctx.exception.status_code, 401)

    async def test_payload_validation(self) -> None:
        tenant = SimpleNamespace(is_enabled=True, discord_config={})

        with self.assertRaises(HTTPException) as exc_ctx:
            await self._call(payload={"command": "!ask"}, tenant=tenant)
        self.assertEqual(exc_ctx.exception.status_code, 400)

        with self.assertRaises(HTTPException) as exc_ctx:
            await self._call(payload={"user_id": "u"}, tenant=tenant)
        self.assertEqual(exc_ctx.exception.status_code, 400)

        with self.assertRaises(HTTPException) as exc_ctx:
            await self._call(payload={"user_id": "u", "command": "!ask", "channel_id": 1}, tenant=tenant)
        self.assertEqual(exc_ctx.exception.status_code, 400)

    async def test_success_enqueues_webhook_job(self) -> None:
        tenant = SimpleNamespace(is_enabled=True, discord_config={})
        response, session, patched = await self._call(
            payload={"user_id": "  user1 ", "command": " !ask status ", "channel_id": " c1 "},
            tenant=tenant,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"accepted":true', response.body)
        self.assertIn(b'"queued":true', response.body)
        patched["enqueue_webhook_job"].assert_called_once()
        request = patched["enqueue_webhook_job"].call_args.kwargs["request"]
        self.assertEqual(request.transport, "discord_webhook")
        self.assertEqual(request.subject_key, "discord_channel:example:c1")
        self.assertEqual(request.payload_json["command"], "!ask status")
        self.assertEqual(request.payload_json["channel_id"], "c1")
        self.assertEqual(request.payload_json["user_id"], "user1")
        patched["notify_webhook_job_enqueued"].assert_called_once()
        session.commit.assert_called_once()

    async def test_seed_command_sets_seed_flag_in_context(self) -> None:
        tenant = SimpleNamespace(is_enabled=True, discord_config={})
        _, _, patched = await self._call(
            payload={"user_id": "user1", "command": "!issues   seed   build stories", "channel_id": "c1"},
            tenant=tenant,
        )
        request = patched["enqueue_webhook_job"].call_args.kwargs["request"]
        self.assertTrue(request.context_json["defer_seed_issues"])

    async def test_followup_context_subject_takes_precedence(self) -> None:
        tenant = SimpleNamespace(is_enabled=True, discord_config={})
        _, _, patched = await self._call(
            payload={"user_id": "user1", "command": "!reply", "channel_id": "c1"},
            tenant=tenant,
            resolve_followup_context=MagicMock(return_value=SimpleNamespace(context_id="ctx-1")),
        )
        request = patched["enqueue_webhook_job"].call_args.kwargs["request"]
        self.assertEqual(request.subject_key, "discord_followup:ctx-1")


if __name__ == "__main__":
    unittest.main()
