from __future__ import annotations

import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from orchestrator.api.routes.webhook_discord import _run_discord_webhook_command, ingest_discord_webhook

pytestmark = pytest.mark.contract


class DiscordWebhookRouteTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, payload: dict, *, tenant=None, headers=None, **overrides):
        request = SimpleNamespace(headers=headers or {})
        session = MagicMock()
        session.get.return_value = tenant
        tasks: list[asyncio.Task[object]] = []

        def _schedule_and_track(coro):  # noqa: ANN001
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task

        base = {
            "get_settings": MagicMock(return_value=SimpleNamespace(secrets_encryption_key="k")),
            "_read_json_payload": AsyncMock(return_value=(payload, b"{}")),
            "_extract_webhook_token": MagicMock(return_value="token"),
            "resolve_scoped_secret_ref": MagicMock(return_value="token"),
            "execute_discord_ingress_command": MagicMock(return_value=SimpleNamespace(model_dump=lambda: {"ok": True})),
            "asyncio": SimpleNamespace(create_task=MagicMock(side_effect=_schedule_and_track)),
        }
        base.update(overrides)

        with ExitStack() as stack:
            for name, value in base.items():
                stack.enter_context(patch(f"orchestrator.api.routes.webhook_discord.{name}", value))
            response = await ingest_discord_webhook("example", request=request, session=session)
            if tasks:
                await asyncio.gather(*tasks)
            return response

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
        create_task_mock = MagicMock(side_effect=lambda coro: asyncio.create_task(coro))
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
        create_task_mock = MagicMock(side_effect=lambda coro: asyncio.create_task(coro))
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
                tenant_id="example",
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
                tenant_id="example",
                payload=payload,
                defer_seed_issues=False,
            )

        self.assertFalse(execute_mock.call_args.kwargs["defer_seed_issues"])
        session.close.assert_called_once()

    async def test_deferred_runner_reraises_execution_errors(self) -> None:
        session = MagicMock()
        session_factory = MagicMock(return_value=session)
        payload = SimpleNamespace(user_id="user-1", command="!status", channel_id="channel-1")
        execute_mock = MagicMock(side_effect=RuntimeError("execution failed"))

        with (
            patch("orchestrator.api.routes.webhook_discord.create_session_factory", return_value=session_factory),
            patch("orchestrator.api.routes.webhook_discord.execute_discord_ingress_command", execute_mock),
            patch("orchestrator.api.routes.webhook_discord.emit_hard_error"),
        ):
            with self.assertRaises(RuntimeError):
                await _run_discord_webhook_command(
                    tenant_id="example",
                    payload=payload,
                    defer_seed_issues=False,
                )

        session.close.assert_called_once()

    async def test_ingest_webhook_routes_task_with_done_callback(self) -> None:
        tenant = SimpleNamespace(is_enabled=True, discord_config={})
        add_done_callback_mock = MagicMock()
        def _capture_task(coro):
            task = asyncio.create_task(coro)
            original_add_done_callback = task.add_done_callback

            def _wrapped(callback):  # noqa: ANN001
                add_done_callback_mock(callback)
                return original_add_done_callback(callback)

            task.add_done_callback = _wrapped  # type: ignore[method-assign]
            return task

        response = await self._call(
            payload={"user_id": "user-1", "command": "!help", "channel_id": "channel-1"},
            tenant=tenant,
            asyncio=SimpleNamespace(
                create_task=MagicMock(side_effect=_capture_task),
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers.get("content-type", "").startswith("application/json"))
        self.assertTrue(add_done_callback_mock.called)
        self.assertIn(b"accepted", response.body)

if __name__ == "__main__":
    unittest.main()
