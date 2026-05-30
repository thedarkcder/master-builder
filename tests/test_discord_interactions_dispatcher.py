from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.responses import JSONResponse

from orchestrator.api.discord.interactions.dispatcher import (
    DiscordInteractionDispatchDeps,
    dispatch_discord_interaction,
)


pytestmark = pytest.mark.contract


def _json_response(*, content: str, ephemeral: bool) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"content": content, "ephemeral": ephemeral},
    )


class DiscordInteractionDispatcherTests(unittest.IsolatedAsyncioTestCase):
    def _deps(self) -> DiscordInteractionDispatchDeps:
        return DiscordInteractionDispatchDeps(
            transport_source="discord_http",
            ask_reply_open_custom_id="ask.reply.open",
            autocomplete_response=lambda **kwargs: JSONResponse(status_code=200, content=kwargs),
            interaction_response=_json_response,
            interaction_modal_response=lambda **kwargs: JSONResponse(status_code=200, content=kwargs),
            interaction_deferred_response=lambda **kwargs: JSONResponse(status_code=200, content=kwargs),
            parse_ask_confirmation_custom_id=lambda _value: None,
            parse_install_request_decision_custom_id=lambda _value: None,
            parse_ask_reply_modal_custom_id=lambda _value: "123456789012345",
            discord_modal_text_value=lambda *_args, **_kwargs: "Need product clarification",
            find_tenant_for_discord_channel=lambda **_kwargs: SimpleNamespace(tenant_id="route25"),
            find_focused_discord_option=lambda _options: None,
            discord_issue_autocomplete_choices=lambda **_kwargs: [],
            resolve_thread_channel_for_reply=lambda **_kwargs: "thread-1",
            resolve_followup_context_match=lambda **_kwargs: SimpleNamespace(
                status="ambiguous",
                context=None,
                matches=(SimpleNamespace(context_id="ctx-1"), SimpleNamespace(context_id="ctx-2")),
            ),
            resolve_followup_context=lambda **_kwargs: None,
            resolve_followup_reaction=lambda **_kwargs: None,
            run_discord_ask_confirmation_followup=MagicMock(),
            run_project_install_request_decision_followup=MagicMock(),
            run_discord_command_followup=MagicMock(),
            run_discord_decision_gate_reply_followup=MagicMock(),
            run_discord_application_command_followup=MagicMock(),
            task_scheduler=MagicMock(),
            logger=MagicMock(),
        )

    async def test_modal_submit_returns_controlled_error_for_ambiguous_followup_context(self) -> None:
        deps = self._deps()

        response = await dispatch_discord_interaction(
            payload={
                "type": 5,
                "application_id": "app-1",
                "token": "token-1",
                "channel_id": "discord-channel-1",
                "data": {"custom_id": "ask.reply.123456789012345", "components": []},
                "user": {"id": "u-1"},
            },
            session=MagicMock(),
            request_id="req-1",
            deps=deps,
        )

        body = json.loads(response.body)
        self.assertIn("multiple active follow-up contexts", body["content"])
        self.assertTrue(body["ephemeral"])
        deps.task_scheduler.assert_not_called()

    async def test_install_request_button_queues_install_decision_followup(self) -> None:
        deps = self._deps()
        deps = SimpleNamespace(
            **{
                **deps.__dict__,
                "parse_install_request_decision_custom_id": lambda _value: ("approve", "a" * 32),
            }
        )

        response = await dispatch_discord_interaction(
            payload={
                "type": 3,
                "application_id": "app-1",
                "token": "token-1",
                "channel_id": "discord-channel-1",
                "data": {"custom_id": f"install_request.approve.{'a' * 32}"},
                "user": {"id": "u-1"},
            },
            session=MagicMock(),
            request_id="req-1",
            deps=deps,  # type: ignore[arg-type]
        )

        self.assertEqual(response.status_code, 200)
        deps.run_project_install_request_decision_followup.assert_called_once_with(
            tenant_id=None,
            user_id="u-1",
            channel_id="discord-channel-1",
            decision="approve",
            request_id="a" * 32,
            application_id="app-1",
            interaction_token="token-1",
        )
        deps.task_scheduler.assert_called_once()


if __name__ == "__main__":
    unittest.main()
