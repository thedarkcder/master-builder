from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from orchestrator.api.routes.webhook_discord import _run_discord_webhook_command, ingest_discord_webhook


class DiscordWebhookRouteTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, payload: dict, *, tenant=None, headers=None, **overrides):
        request = SimpleNamespace(headers=headers or {})
        session = MagicMock()
        session.get.return_value = tenant

        def _capture_and_close(coro):  # noqa: ANN001
            coro.close()
            return MagicMock()

        base = {
            "get_settings": MagicMock(return_value=SimpleNamespace(secrets_encryption_key="k")),
            "_read_json_payload": AsyncMock(return_value=(payload, b"{}")),
            "_extract_webhook_token": MagicMock(return_value="token"),
            "resolve_scoped_secret_ref": MagicMock(return_value="token"),
            "execute_discord_ingress_command": MagicMock(return_value=SimpleNamespace(model_dump=lambda: {"ok": True})),
            "asyncio": SimpleNamespace(create_task=MagicMock(side_effect=_capture_and_close)),
        }
        base.update(overrides)

        with ExitStack() as stack:
            for name, value in base.items():
                stack.enter_context(patch(f"orchestrator.api.routes.webhook_discord.{name}", value))
            return await ingest_discord_webhook("route25", request=request, session=session)

    async def test_unknown_tenant(self) -> None:
        with self.assertRaises(HTTPException) as exc_ctx:
            await self._call(payload={}, tenant=None)
        self.assertEqual(exc_ctx.exception.status_code, 404)

    async def test_tenant_disabled(self) -> None:
        response = await self._call(payload={}, tenant=SimpleNamespace(is_enabled=False, discord_config={}))
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

    async def test_success(self) -> None:
        tenant = SimpleNamespace(is_enabled=True, discord_config={})
        create_task_mock = MagicMock(side_effect=lambda coro: (coro.close(), MagicMock())[1])
        response = await self._call(
            payload={"user_id": "  user1 ", "command": " !ask status ", "channel_id": " c1 "},
            tenant=tenant,
            asyncio=SimpleNamespace(create_task=create_task_mock),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"accepted":true', response.body)
        self.assertIn(b'"deferred":true', response.body)
        create_task_mock.assert_called_once()

    async def test_seed_command_defers_with_repeated_whitespace(self) -> None:
        tenant = SimpleNamespace(is_enabled=True, discord_config={})
        create_task_mock = MagicMock(side_effect=lambda coro: (coro.close(), MagicMock())[1])
        response = await self._call(
            payload={"user_id": "user1", "command": "!issues   seed   build stories", "channel_id": "c1"},
            tenant=tenant,
            asyncio=SimpleNamespace(create_task=create_task_mock),
        )
        self.assertEqual(response.status_code, 200)
        create_task_mock.assert_called_once()

    async def test_deferred_runner_sets_seed_flag_for_issue_seed_command(self) -> None:
        session = MagicMock()
        session_factory = MagicMock(return_value=session)
        payload = SimpleNamespace(user_id="user-1", command="!issues seed Draft", channel_id="channel-1")
        execute_mock = MagicMock()

        with (
            patch("orchestrator.api.routes.webhook_discord.create_session_factory", return_value=session_factory),
            patch("orchestrator.api.routes.webhook_discord.execute_discord_ingress_command", execute_mock),
        ):
            await _run_discord_webhook_command(
                tenant_id="route25",
                payload=payload,
                defer_seed_issues=True,
            )

        self.assertTrue(execute_mock.call_args.kwargs["defer_seed_issues"])
        session.close.assert_called_once()

    async def test_deferred_runner_clears_seed_flag_for_non_seed_command(self) -> None:
        session = MagicMock()
        session_factory = MagicMock(return_value=session)
        payload = SimpleNamespace(user_id="user-1", command="!status", channel_id="channel-1")
        execute_mock = MagicMock()

        with (
            patch("orchestrator.api.routes.webhook_discord.create_session_factory", return_value=session_factory),
            patch("orchestrator.api.routes.webhook_discord.execute_discord_ingress_command", execute_mock),
        ):
            await _run_discord_webhook_command(
                tenant_id="route25",
                payload=payload,
                defer_seed_issues=False,
            )

        self.assertFalse(execute_mock.call_args.kwargs["defer_seed_issues"])
        session.close.assert_called_once()

if __name__ == "__main__":
    unittest.main()
