from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.api.discord.messages.application import DiscordMessageIngressDeps, build_discord_message_ingress_result
from orchestrator.core.communications import DiscordAskWithThreadAction, DiscordThreadReplyAction


class DiscordMessageApplicationTests(unittest.TestCase):
    def _deps(
        self,
        *,
        execute_tenant_discord_command,
        resolve_followup_context_match,
        resolve_followup_reaction=None,
        transcribe_audio_attachment=None,
        room_voice_reply_enabled=False,
        build_room_voice_reply_action=None,
        route_voice_entry=None,
        execute_voice_room_persona_voice_note=None,
    ) -> DiscordMessageIngressDeps:
        logger = MagicMock()
        return DiscordMessageIngressDeps(
            find_tenant_for_channel=MagicMock(return_value=SimpleNamespace(tenant_id="tenant-1", discord_config={})),
            resolve_project_for_discord_channel=MagicMock(
                return_value=SimpleNamespace(project_id="project-1", jira_project_key="TP", discord_config={})
            ),
            project_room_channel_ids=MagicMock(return_value=set()),
            room_channel_ids_from_discord_config=MagicMock(return_value=set()),
            is_audio_attachment=MagicMock(side_effect=lambda attachment: str(attachment.get("content_type") or "").startswith("audio/")),
            transcribe_audio_attachment=transcribe_audio_attachment or MagicMock(return_value=("Create a share feature", None)),
            load_pending_human_input_request=MagicMock(return_value=None),
            resume_run_from_human_input_reply=MagicMock(),
            resolve_followup_context_match=resolve_followup_context_match,
            resolve_followup_context=MagicMock(return_value=None),
            resolve_followup_reaction=resolve_followup_reaction or MagicMock(return_value=None),
            execute_tenant_discord_command=execute_tenant_discord_command,
            resolve_tenant_jira_browse_base_url=MagicMock(return_value=None),
            build_command_followup_message=MagicMock(return_value="PM guidance"),
            build_ask_confirmation_components=MagicMock(return_value=[{"type": 1}]),
            ask_reply_components=MagicMock(return_value=[{"type": 1}]),
            issue_key_pattern=MagicMock(),
            room_voice_reply_enabled=MagicMock(return_value=room_voice_reply_enabled),
            build_room_voice_reply_action=build_room_voice_reply_action or MagicMock(return_value=(None, None)),
            emit_hard_error=MagicMock(),
            logger=logger,
            settings=SimpleNamespace(),
            route_voice_entry=route_voice_entry
            or MagicMock(return_value={"lane": "pm", "persona": "pm", "confidence": 1.0, "reason": "default"}),
            execute_voice_room_persona_voice_note=execute_voice_room_persona_voice_note or MagicMock(),
        )

    def test_pm_command_opens_interview_thread(self) -> None:
        execute = MagicMock(
            return_value=SimpleNamespace(
                ok=True,
                command="pm",
                message="PM guidance",
                data={"pm_mode": True, "followup_context_type": "pm_interview", "issue_key": "TP-1"},
            )
        )
        deps = self._deps(
            execute_tenant_discord_command=execute,
            resolve_followup_context_match=MagicMock(return_value=SimpleNamespace(status="no_match", context=None, matches=())),
        )

        result = build_discord_message_ingress_result(
            payload={
                "id": "msg-1",
                "channel_id": "root-1",
                "author": {"id": "user-1", "bot": False},
                "content": "!pm create a share feature",
            },
            session=MagicMock(),
            deps=deps,
        )

        self.assertEqual(len(result.actions), 1)
        self.assertIsInstance(result.actions[0], DiscordAskWithThreadAction)
        self.assertEqual(result.actions[0].followup_context_type, "pm_interview")
        self.assertEqual(result.actions[0].content, "PM guidance")
        self.assertEqual(execute.call_args.kwargs["payload"].command, "!pm create a share feature")

    def test_pm_thread_reply_routes_back_to_pm_command(self) -> None:
        execute = MagicMock(
            return_value=SimpleNamespace(
                ok=True,
                command="pm",
                message="PM guidance",
                data={"pm_mode": True, "followup_context_type": "pm_interview", "issue_key": "TP-1"},
            )
        )
        deps = self._deps(
            execute_tenant_discord_command=execute,
            resolve_followup_context_match=MagicMock(
                return_value=SimpleNamespace(
                    status="matched",
                    context=SimpleNamespace(context_type="pm_interview", origin_command="pm"),
                    matches=(SimpleNamespace(context_type="pm_interview", origin_command="pm"),),
                )
            ),
        )

        result = build_discord_message_ingress_result(
            payload={
                "id": "msg-2",
                "channel_id": "thread-1",
                "author": {"id": "user-1", "bot": False},
                "content": "Should it share a link or invite?",
            },
            session=MagicMock(),
            deps=deps,
        )

        self.assertEqual(len(result.actions), 1)
        self.assertIsInstance(result.actions[0], DiscordThreadReplyAction)
        self.assertEqual(result.actions[0].channel_id, "thread-1")
        self.assertEqual(execute.call_args.kwargs["payload"].command, "!pm Should it share a link or invite?")

    def test_plain_root_text_without_followup_is_ignored(self) -> None:
        execute = MagicMock()
        deps = self._deps(
            execute_tenant_discord_command=execute,
            resolve_followup_context_match=MagicMock(return_value=SimpleNamespace(status="no_match", context=None, matches=())),
        )

        result = build_discord_message_ingress_result(
            payload={
                "id": "msg-3",
                "channel_id": "root-1",
                "author": {"id": "user-1", "bot": False},
                "content": "create a share feature",
            },
            session=MagicMock(),
            deps=deps,
        )

        self.assertEqual(result.actions, ())
        execute.assert_not_called()

    def test_pm_voice_note_entry_opens_interview_thread(self) -> None:
        execute = MagicMock(
            return_value=SimpleNamespace(
                ok=True,
                command="pm",
                message="PM guidance",
                data={"pm_mode": True, "followup_context_type": "pm_interview", "issue_key": "TP-1"},
            )
        )
        deps = self._deps(
            execute_tenant_discord_command=execute,
            resolve_followup_context_match=MagicMock(return_value=SimpleNamespace(status="no_match", context=None, matches=())),
            transcribe_audio_attachment=MagicMock(return_value=("create a share feature", None)),
        )

        result = build_discord_message_ingress_result(
            payload={
                "id": "msg-4",
                "channel_id": "root-1",
                "author": {"id": "user-1", "bot": False},
                "content": "",
                "attachments": [
                    {
                        "id": "att-1",
                        "url": "https://example.com/note.ogg",
                        "filename": "note.ogg",
                        "content_type": "audio/ogg",
                        "size": "100",
                    }
                ],
            },
            session=MagicMock(),
            deps=deps,
        )

        self.assertEqual(len(result.actions), 1)
        self.assertIsInstance(result.actions[0], DiscordAskWithThreadAction)
        self.assertEqual(result.actions[0].followup_context_type, "pm_interview")
        self.assertEqual(execute.call_args.kwargs["payload"].command, "!pm create a share feature")

    def test_voice_note_router_maps_persona_pm_to_voice_room_persona_executor(self) -> None:
        execute = MagicMock()
        persona_execute = MagicMock(
            return_value=SimpleNamespace(
                ok=True,
                command="voice_room_persona",
                message="Persona reply",
                data={
                    "persona_id": "pm",
                    "persona_name": "Andy",
                    "persona_role": "PM",
                },
            )
        )
        route = MagicMock(return_value={"lane": "persona", "persona": "pm", "confidence": 0.8, "reason": "routing"})
        deps = self._deps(
            execute_tenant_discord_command=execute,
            resolve_followup_context_match=MagicMock(return_value=SimpleNamespace(status="no_match", context=None, matches=())),
            transcribe_audio_attachment=MagicMock(return_value=("hello from voice", None)),
            route_voice_entry=route,
            execute_voice_room_persona_voice_note=persona_execute,
        )

        result = build_discord_message_ingress_result(
            payload={
                "id": "msg-5",
                "channel_id": "root-1",
                "author": {"id": "user-1", "bot": False},
                "content": "",
                "attachments": [
                    {
                        "id": "att-2",
                        "url": "https://example.com/note2.ogg",
                        "filename": "note2.ogg",
                        "content_type": "audio/ogg",
                        "size": "100",
                    }
                ],
            },
            session=MagicMock(),
            deps=deps,
        )

        execute.assert_not_called()
        persona_execute.assert_called_once()
        self.assertEqual(persona_execute.call_args.kwargs["transcript"], "hello from voice")
        self.assertEqual(persona_execute.call_args.kwargs["persona_id"], "pm")
        self.assertEqual(len(result.actions), 1)

