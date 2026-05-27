from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.api.routes.webhook_discord_interactions import (
    _resolve_interaction_subject_scope,
    ingest_discord_interaction,
)
from orchestrator.core.communications import (
    DeferredTransportWork,
    HttpJsonResponseBytesAction,
    IngressResult,
)

pytestmark = pytest.mark.contract


class DiscordInteractionsRouteTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _result(*, body: bytes, deferred: bool) -> IngressResult:
        async def _noop() -> None:
            return None

        deferred_work = ()
        if deferred:
            deferred_work = (
                DeferredTransportWork(
                    kind="test",
                    runner=lambda: _noop(),
                ),
            )
        return IngressResult(
            actions=(
                HttpJsonResponseBytesAction(
                    status_code=200,
                    body=body,
                    headers={"content-type": "application/json"},
                ),
            ),
            deferred_work=deferred_work,
        )

    async def _call(self, payload: dict, **overrides):
        request = SimpleNamespace(headers={})
        session = MagicMock()
        session.get.return_value = None
        base = {
            "get_settings": MagicMock(return_value=SimpleNamespace()),
            "_read_json_payload": AsyncMock(return_value=(payload, b"{}")),
            "_resolve_discord_interactions_public_key": MagicMock(return_value=b"k"),
            "_validate_discord_interaction_signature": MagicMock(),
            "enqueue_webhook_job": MagicMock(
                return_value=SimpleNamespace(
                    created=True,
                    job=SimpleNamespace(job_id="job-1", dedupe_key=str(payload.get("id") or "").strip() or None, subject_key="discord_interaction:unknown"),
                )
            ),
            "notify_webhook_job_enqueued": MagicMock(),
            "_resolve_interaction_subject_scope": MagicMock(return_value=("route25", None, "discord_channel:route25:c1")),
            "_close_deferred_interaction_work": MagicMock(),
            "build_discord_interaction_ingress_result": AsyncMock(
                return_value=self._result(body=b'{"type":1}', deferred=False)
            ),
        }
        base.update(overrides)

        with ExitStack() as stack:
            for name, value in base.items():
                stack.enter_context(patch(f"orchestrator.api.routes.webhook_discord_interactions.{name}", value))
            response = await ingest_discord_interaction(request=request, session=session)
        return response, session, base

    async def test_ping_and_unsupported_type_do_not_enqueue(self) -> None:
        ping, _, patched = await self._call(
            {"type": 1},
            build_discord_interaction_ingress_result=AsyncMock(
                return_value=self._result(body=b'{"type":1}', deferred=False)
            ),
        )
        self.assertEqual(ping.status_code, 200)
        patched["enqueue_webhook_job"].assert_not_called()

        unsupported, _, patched = await self._call(
            {"type": 999},
            build_discord_interaction_ingress_result=AsyncMock(
                return_value=self._result(
                    body=b'{"type":4,"data":{"content":"Unsupported Discord interaction type.","flags":64}}',
                    deferred=False,
                )
            ),
        )
        self.assertEqual(unsupported.status_code, 200)
        self.assertIn(b"Unsupported Discord interaction type", unsupported.body)
        patched["enqueue_webhook_job"].assert_not_called()

    async def test_application_command_acknowledges_and_enqueues(self) -> None:
        response, session, patched = await self._call(
            {
                "type": 2,
                "application_id": "app",
                "token": "tok",
                "channel_id": "c1",
                "data": {"name": "ask"},
                "member": {"user": {"id": "u-1"}},
            },
            build_discord_interaction_ingress_result=AsyncMock(
                return_value=self._result(body=b'{"type":5,"data":{"flags":64}}', deferred=True)
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b'{"type":5,"data":{"flags":64}}')
        patched["enqueue_webhook_job"].assert_called_once()
        request = patched["enqueue_webhook_job"].call_args.kwargs["request"]
        self.assertEqual(request.transport, "discord_interaction")
        self.assertEqual(request.event_type, "interaction_create")
        self.assertEqual(request.payload_json["data"]["name"], "ask")
        patched["notify_webhook_job_enqueued"].assert_called_once()
        session.commit.assert_called_once()

    async def test_application_command_with_unresolved_scope_returns_discord_error_without_enqueue(self) -> None:
        response, session, patched = await self._call(
            {
                "type": 2,
                "id": "interaction-1",
                "application_id": "app",
                "token": "tok",
                "channel_id": "thread-1",
                "data": {"name": "run"},
                "member": {"user": {"id": "u-1"}},
            },
            build_discord_interaction_ingress_result=AsyncMock(
                return_value=self._result(body=b'{"type":5,"data":{"flags":64}}', deferred=True)
            ),
            _resolve_interaction_subject_scope=MagicMock(return_value=(None, None, "discord_channel::thread-1")),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"could not map this Discord channel", response.body)
        patched["enqueue_webhook_job"].assert_not_called()
        patched["notify_webhook_job_enqueued"].assert_not_called()
        session.commit.assert_not_called()
        patched["_close_deferred_interaction_work"].assert_called_once()

    async def test_modal_submit_acknowledges_and_enqueues(self) -> None:
        response, session, patched = await self._call(
            {
                "type": 5,
                "application_id": "app",
                "token": "tok",
                "channel_id": "c1",
                "data": {"custom_id": "ask.reply.m1", "components": []},
                "user": {"id": "u-1"},
            },
            build_discord_interaction_ingress_result=AsyncMock(
                return_value=self._result(body=b'{"type":5,"data":{"flags":64}}', deferred=True)
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b'{"type":5,"data":{"flags":64}}')
        patched["enqueue_webhook_job"].assert_called_once()
        request = patched["enqueue_webhook_job"].call_args.kwargs["request"]
        self.assertEqual(request.transport, "discord_interaction")
        patched["notify_webhook_job_enqueued"].assert_called_once()
        session.commit.assert_called_once()

    async def test_autocomplete_does_not_enqueue(self) -> None:
        response, _, patched = await self._call(
            {
                "type": 4,
                "channel_id": "c1",
                "data": {
                    "name": "run",
                    "options": [{"name": "issue_key", "value": "TP", "focused": True}],
                },
            },
            build_discord_interaction_ingress_result=AsyncMock(
                return_value=self._result(body=b'{"type":8,"data":{"choices":[]}}', deferred=False)
            ),
        )
        self.assertEqual(response.status_code, 200)
        patched["enqueue_webhook_job"].assert_not_called()

    def test_resolve_interaction_subject_scope_uses_top_level_user_for_dm_payloads(self) -> None:
        deps = SimpleNamespace(find_tenant_for_discord_channel=MagicMock(return_value=None))
        with patch(
            "orchestrator.api.routes.webhook_discord_interactions.build_default_discord_interaction_dispatch_deps",
            return_value=deps,
        ):
            tenant_id, project_id, subject_key = _resolve_interaction_subject_scope(
                session=MagicMock(),
                payload={
                    "type": 5,
                    "user": {"id": "u-1"},
                },
                find_tenant_for_discord_channel=deps.find_tenant_for_discord_channel,
            )

        self.assertIsNone(tenant_id)
        self.assertIsNone(project_id)
        self.assertEqual(subject_key, "discord_user::u-1")


if __name__ == "__main__":
    unittest.main()
