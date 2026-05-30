from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.api.discord.messages.application import DiscordMessageIngressDeps, build_discord_message_ingress_result
from orchestrator.core.communications import (
    DiscordAskWithThreadAction,
    DiscordChannelMessageAction,
    DiscordChannelMessageWithAttachmentAction,
    DiscordThreadReplyAction,
)
from orchestrator.core.runtime.payload_models import VoiceEntryRoute


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
            answer_human_input_request=MagicMock(),
            resume_workflow_from_human_input_answer=MagicMock(),
            load_project_install_request=MagicMock(),
            approve_install_request=MagicMock(),
            reject_install_request=MagicMock(),
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
            or MagicMock(
                return_value=VoiceEntryRoute(
                    lane="interview",
                    persona="pm",
                    confidence=1.0,
                    reason="default",
                )
            ),
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

    def test_human_input_reply_answers_then_resumes_workflow(self) -> None:
        execute = MagicMock()
        pending_request = SimpleNamespace(
            request_id="request-1",
            issue_key="TP-1",
        )
        answered_request = SimpleNamespace(request_id="request-1")
        resumed_run = SimpleNamespace(run_id="run-2")
        deps = self._deps(
            execute_tenant_discord_command=execute,
            resolve_followup_context_match=MagicMock(
                return_value=SimpleNamespace(status="matched", context=SimpleNamespace(context_type="human_input"), matches=())
            ),
            resolve_followup_reaction=MagicMock(
                return_value=SimpleNamespace(kind="human_input", request_id="request-1")
            ),
        )
        deps.load_pending_human_input_request.return_value = pending_request
        deps.answer_human_input_request.return_value = answered_request
        deps.resume_workflow_from_human_input_answer.return_value = resumed_run

        result = build_discord_message_ingress_result(
            payload={
                "id": "msg-4a",
                "channel_id": "thread-1",
                "author": {"id": "user-1", "bot": False},
                "content": "use qa-account@example.com",
            },
            session=MagicMock(),
            deps=deps,
        )

        self.assertEqual(len(result.actions), 1)
        self.assertIsInstance(result.actions[0], DiscordChannelMessageAction)
        deps.answer_human_input_request.assert_called_once()
        deps.resume_workflow_from_human_input_answer.assert_called_once_with(
            session=unittest.mock.ANY,
            settings=deps.settings,
            request=answered_request,
        )

    def test_install_request_yes_approves_instead_of_generic_human_input_resume(self) -> None:
        execute = MagicMock()
        pending_request = SimpleNamespace(
            request_id="human-input-1",
            issue_key="AP-248",
            request_type="install_request",
            request_context_json={"install_request_id": "install-request-1"},
        )
        install_request = SimpleNamespace(
            request_id="install-request-1",
            issue_key="AP-248",
            project_id="project-1",
        )
        deps = self._deps(
            execute_tenant_discord_command=execute,
            resolve_followup_context_match=MagicMock(
                return_value=SimpleNamespace(status="matched", context=SimpleNamespace(context_type="human_input"), matches=())
            ),
            resolve_followup_reaction=MagicMock(
                return_value=SimpleNamespace(kind="human_input", request_id="human-input-1")
            ),
        )
        deps.load_pending_human_input_request.return_value = pending_request
        deps.load_project_install_request.return_value = install_request

        result = build_discord_message_ingress_result(
            payload={
                "id": "msg-install-approve",
                "channel_id": "thread-1",
                "author": {"id": "user-1", "bot": False},
                "content": "yes",
            },
            session=MagicMock(get=MagicMock(return_value=SimpleNamespace(project_id="project-1"))),
            deps=deps,
        )

        self.assertEqual(len(result.actions), 1)
        self.assertIn("Approved the install request", result.actions[0].content)
        deps.approve_install_request.assert_called_once()
        deps.answer_human_input_request.assert_not_called()
        deps.resume_workflow_from_human_input_answer.assert_not_called()

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

    def test_voice_note_router_ask_lane_dispatches_ask_with_router_persona(self) -> None:
        execute = MagicMock(
            return_value=SimpleNamespace(
                ok=True,
                command="ask",
                message="Persona reply",
                data={
                    "persona_id": "pm",
                    "persona_name": "Andy",
                    "persona_role": "PM",
                    "room_mode": True,
                    "room_source": "voice_note",
                },
            )
        )
        route = MagicMock(return_value=VoiceEntryRoute(lane="ask", persona="pm", confidence=0.8, reason="routing"))
        deps = self._deps(
            execute_tenant_discord_command=execute,
            resolve_followup_context_match=MagicMock(return_value=SimpleNamespace(status="no_match", context=None, matches=())),
            transcribe_audio_attachment=MagicMock(return_value=("hello from voice", None)),
            route_voice_entry=route,
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

        execute.assert_called_once()
        self.assertEqual(execute.call_args.kwargs["payload"].command, "!ask hello from voice")
        self.assertEqual(
            execute.call_args.kwargs["payload"].command_params,
            {
                "room_mode": "true",
                "room_source": "voice_note",
                "persona_id": "pm",
            },
        )
        self.assertEqual(len(result.actions), 1)

    def test_voice_note_router_interview_lane_dispatches_pm(self) -> None:
        execute = MagicMock(
            return_value=SimpleNamespace(
                ok=True,
                command="pm",
                message="PM guidance",
                data={"pm_mode": True, "followup_context_type": "pm_interview", "issue_key": "TP-1"},
            )
        )
        route = MagicMock(return_value=VoiceEntryRoute(lane="interview", persona="pm", confidence=0.9, reason="brief"))
        deps = self._deps(
            execute_tenant_discord_command=execute,
            resolve_followup_context_match=MagicMock(return_value=SimpleNamespace(status="no_match", context=None, matches=())),
            transcribe_audio_attachment=MagicMock(return_value=("scope the dashboard", None)),
            route_voice_entry=route,
        )

        build_discord_message_ingress_result(
            payload={
                "id": "msg-6",
                "channel_id": "root-1",
                "author": {"id": "user-1", "bot": False},
                "content": "",
                "attachments": [
                    {
                        "id": "att-3",
                        "url": "https://example.com/note3.ogg",
                        "filename": "note3.ogg",
                        "content_type": "audio/ogg",
                        "size": "100",
                    }
                ],
            },
            session=MagicMock(),
            deps=deps,
        )

        execute.assert_called_once()
        self.assertEqual(execute.call_args.kwargs["payload"].command, "!pm scope the dashboard")
        self.assertEqual(
            execute.call_args.kwargs["payload"].command_params,
            {"room_mode": "true", "room_source": "voice_note"},
        )

    def test_ask_voice_note_room_voice_reply_single_attachment_message(self) -> None:
        """Ask + voice note + room TTS: one channel action (text + wav), not text then attachment duplicate."""
        attachment_action = DiscordChannelMessageWithAttachmentAction(
            channel_id="root-1",
            content="<@user-1> spoken reply",
            filename="voice_reply.wav",
            file_bytes=b"RIFF",
            content_type="audio/wav",
        )
        build_voice = MagicMock(return_value=(attachment_action, None))
        execute = MagicMock(
            return_value=SimpleNamespace(
                ok=True,
                command="ask",
                message="Spoken reply body",
                data={
                    "persona_id": "pm",
                    "persona_name": "Andy",
                    "persona_role": "PM",
                    "room_mode": True,
                    "room_source": "voice_note",
                },
            )
        )
        route = MagicMock(return_value=VoiceEntryRoute(lane="ask", persona="pm", confidence=0.9, reason="ask"))
        deps = self._deps(
            execute_tenant_discord_command=execute,
            resolve_followup_context_match=MagicMock(return_value=SimpleNamespace(status="no_match", context=None, matches=())),
            transcribe_audio_attachment=MagicMock(return_value=("hello with TTS", None)),
            room_voice_reply_enabled=True,
            build_room_voice_reply_action=build_voice,
            route_voice_entry=route,
        )

        result = build_discord_message_ingress_result(
            payload={
                "id": "msg-tts",
                "channel_id": "root-1",
                "author": {"id": "user-1", "bot": False},
                "content": "",
                "attachments": [
                    {
                        "id": "att-tts",
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
        self.assertIsInstance(result.actions[0], DiscordChannelMessageWithAttachmentAction)
        build_voice.assert_called_once()
        self.assertEqual(build_voice.call_args.kwargs.get("content_override"), "PM guidance")
