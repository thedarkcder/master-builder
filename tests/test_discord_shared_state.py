from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from orchestrator.api.discord.shared import state as state_module


class DiscordSharedStateTests(unittest.TestCase):
    def test_parse_command_text(self) -> None:
        command, args = state_module.parse_command_text("!run MAB-1")
        self.assertEqual(command, "run")
        self.assertEqual(args, ["MAB-1"])

        with self.assertRaises(HTTPException):
            state_module.parse_command_text("run MAB-1")
        with self.assertRaises(HTTPException):
            state_module.parse_command_text("!")
        with self.assertRaises(HTTPException):
            state_module.parse_command_text("!unsupported")

    def test_command_matches_normalizes_internal_whitespace(self) -> None:
        self.assertTrue(
            state_module.command_matches("!issues   seed   draft", command_name="issues", subcommand="seed")
        )
        self.assertTrue(state_module.command_matches("!   ask   status", command_name="ask"))
        self.assertFalse(state_module.command_matches("!issues followup text", command_name="issues", subcommand="seed"))

    def test_allowlist_helpers(self) -> None:
        tenant = SimpleNamespace(discord_config={"allowed_user_ids": ["u1", " "]})
        project = SimpleNamespace(discord_config={"allowed_user_ids": ["u2", "u3"]})
        self.assertEqual(state_module.tenant_allowlisted_user_ids(tenant), {"u1"})
        self.assertEqual(state_module.project_allowlisted_user_ids(project), {"u2", "u3"})

        tenant_no_ts = SimpleNamespace(discord_config={"allowlist_requests": [{"user_id": "u1"}]})
        tenant_requests = state_module.tenant_allowlist_requests(tenant_no_ts)
        self.assertEqual(tenant_requests[0]["user_id"], "u1")
        self.assertIn("requested_at", tenant_requests[0])

        project_no_ts = SimpleNamespace(discord_config={"allowlist_requests": [{"user_id": "u2"}]})
        project_requests = state_module.project_allowlist_requests(project_no_ts)
        self.assertEqual(project_requests[0]["user_id"], "u2")
        self.assertIn("requested_at", project_requests[0])

    def test_create_allowlist_request_paths(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={"allowed_user_ids": ["u1"]})
        project = SimpleNamespace(discord_config={"allowlist_requests": []})

        created, msg = state_module.create_allowlist_request(
            session=session,
            tenant=tenant,
            user_id="u1",
            channel_id="c1",
            permissions=["run_controls"],
            reason=None,
        )
        self.assertFalse(created)
        self.assertIn("already allowlisted", msg)

        tenant = SimpleNamespace(tenant_id="t1", discord_config={})
        with patch("orchestrator.api.discord.shared.state.resolve_project_for_discord_channel", return_value=None):
            created, msg = state_module.create_allowlist_request(
                session=session,
                tenant=tenant,
                user_id="u2",
                channel_id="c1",
                permissions=["run_controls"],
                reason=None,
            )
        self.assertFalse(created)
        self.assertIn("mapped project Discord channel", msg)

        with (
            patch("orchestrator.api.discord.shared.state.resolve_project_for_discord_channel", return_value=project),
            patch("orchestrator.api.discord.shared.state.save_project_allowlist_requests") as save_mock,
        ):
            created, msg = state_module.create_allowlist_request(
                session=session,
                tenant=tenant,
                user_id="u3",
                channel_id="c1",
                permissions=["run_controls"],
                reason="need it",
            )
        self.assertTrue(created)
        self.assertIn("submitted", msg)
        save_mock.assert_called_once()

        project_refresh = SimpleNamespace(discord_config={"allowlist_requests": [{"user_id": "u3", "requested_at": "x"}]})
        with (
            patch("orchestrator.api.discord.shared.state.resolve_project_for_discord_channel", return_value=project_refresh),
            patch("orchestrator.api.discord.shared.state.save_project_allowlist_requests") as save_mock,
        ):
            created, msg = state_module.create_allowlist_request(
                session=session,
                tenant=tenant,
                user_id="u3",
                channel_id="c2",
                permissions=["seed_issues"],
                reason="refresh",
            )
        self.assertTrue(created)
        self.assertIn("refreshed", msg)
        save_mock.assert_called_once()

    def test_assert_sensitive_command_permission(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={})
        project = SimpleNamespace(discord_config={})

        state_module.assert_sensitive_command_permission(
            session=session,
            tenant=tenant,
            command_name="help",
            user_id="u1",
            channel_id="c1",
        )

        with (
            patch("orchestrator.api.discord.shared.state.resolve_project_for_discord_channel", return_value=project),
            patch("orchestrator.api.discord.shared.state.can_execute_sensitive_command", return_value=(True, None)),
        ):
            state_module.assert_sensitive_command_permission(
                session=session,
                tenant=tenant,
                command_name="run",
                user_id="u1",
                channel_id="c1",
            )

        with (
            patch("orchestrator.api.discord.shared.state.resolve_project_for_discord_channel", return_value=None),
            patch("orchestrator.api.discord.shared.state.can_execute_sensitive_command", return_value=(False, "denied")),
        ):
            with self.assertRaises(HTTPException) as exc_ctx:
                state_module.assert_sensitive_command_permission(
                    session=session,
                    tenant=tenant,
                    command_name="run",
                    user_id="u1",
                    channel_id="c1",
                )
        self.assertEqual(exc_ctx.exception.status_code, 403)
        self.assertEqual(exc_ctx.exception.detail, "denied")

    def test_assert_channel_scope(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={})
        with (
            patch("orchestrator.api.discord.shared.state.tenant_allowed_channel_ids", return_value={"c1"}),
            patch("orchestrator.api.discord.shared.state.project_room_channel_ids", return_value={"room-1"}),
            patch("orchestrator.api.discord.shared.state.is_channel_allowed", return_value=True),
        ):
            state_module.assert_channel_scope(session=session, tenant=tenant, channel_id="c1")

        with (
            patch("orchestrator.api.discord.shared.state.tenant_allowed_channel_ids", return_value={"c1"}),
            patch("orchestrator.api.discord.shared.state.project_room_channel_ids", return_value={"room-1"}),
            patch("orchestrator.api.discord.shared.state.is_channel_allowed", return_value=False),
        ):
            with self.assertRaises(HTTPException):
                state_module.assert_channel_scope(session=session, tenant=tenant, channel_id="c2")

        with (
            patch("orchestrator.api.discord.shared.state.tenant_allowed_channel_ids", return_value=set()),
            patch("orchestrator.api.discord.shared.state.project_room_channel_ids", return_value={"room-1"}),
            patch("orchestrator.api.discord.shared.state.is_channel_allowed", return_value=True),
        ):
            state_module.assert_channel_scope(session=session, tenant=tenant, channel_id="room-1")

    def test_room_channel_helpers_support_general_and_legacy_keys(self) -> None:
        config = {
            "voice_room_channel_ids": ["voice-room-1", " "],
            "persona_room_thread_channel_id": "persona-thread-1",
            "pm_room_channel_ids": ["pm-room-1", ""],
        }
        self.assertEqual(
            state_module.room_channel_ids_from_discord_config(config),
            {"voice-room-1", "persona-thread-1", "pm-room-1"},
        )

        session = MagicMock()
        project = SimpleNamespace(discord_config=config)
        session.execute.return_value.scalars.return_value.all.return_value = [project]
        self.assertEqual(
            state_module.project_room_channel_ids(session=session, tenant_id="tenant-1"),
            {"voice-room-1", "persona-thread-1", "pm-room-1"},
        )
        self.assertEqual(
            state_module.project_pm_room_channel_ids(session=session, tenant_id="tenant-1"),
            {"voice-room-1", "persona-thread-1", "pm-room-1"},
        )

    def test_live_voice_room_helpers_support_links_and_enabled(self) -> None:
        config = {
            "live_voice_enabled": "yes",
            "live_voice_room_links": {
                "voice-room-1": "text-room-1",
                " voice-room-2 ": " thread-room-2 ",
                " ": "skip",
            },
        }
        self.assertTrue(state_module.live_voice_enabled_from_discord_config(config))
        self.assertEqual(
            state_module.live_voice_room_links_from_discord_config(config),
            {
                "voice-room-1": "text-room-1",
                "voice-room-2": "thread-room-2",
            },
        )
        self.assertEqual(
            state_module.live_voice_room_channel_ids_from_discord_config(config),
            {"voice-room-1", "voice-room-2"},
        )
        self.assertEqual(
            state_module.live_voice_linked_channel_ids_from_discord_config(config),
            {"text-room-1", "thread-room-2"},
        )

        session = MagicMock()
        project = SimpleNamespace(discord_config=config)
        session.execute.return_value.scalars.return_value.all.return_value = [project]
        self.assertEqual(
            state_module.project_live_voice_room_channel_ids(session=session, tenant_id="tenant-1"),
            {"voice-room-1", "voice-room-2"},
        )
        self.assertEqual(
            state_module.project_live_voice_room_links(session=session, tenant_id="tenant-1"),
            {
                "voice-room-1": "text-room-1",
                "voice-room-2": "thread-room-2",
            },
        )
        self.assertEqual(
            state_module.project_live_voice_linked_channel_ids(session=session, tenant_id="tenant-1"),
            {"text-room-1", "thread-room-2"},
        )

    def test_live_voice_room_helpers_ignore_missing_map_entries(self) -> None:
        config = {
            "live_voice_enabled": True,
            "live_voice_room_links": {
                "voice-room-1": "",
                " ": "text-room-2",
            },
        }
        self.assertTrue(state_module.live_voice_enabled_from_discord_config(config))
        self.assertEqual(state_module.live_voice_room_links_from_discord_config(config), {})
        self.assertEqual(state_module.live_voice_room_channel_ids_from_discord_config(config), set())
        self.assertEqual(state_module.live_voice_linked_channel_ids_from_discord_config(config), set())

    def test_seed_followup_lifecycle(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(discord_config={}, updated_at=None)

        request_id = state_module.store_seed_followup_context(
            session=session,
            tenant=tenant,
            request_id=None,
            user_id="u1",
            channel_ids=["c1", " ", "c2"],
            project_id="example-default",
            project_key="mab",
            issue_keys=["mab-1", "", "MAB-2"],
            questions=["q1", " ", "q2"],
            prompt_markdown="prompt",
        )
        self.assertTrue(request_id)
        entries = (tenant.discord_config or {}).get("seed_followups", [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["project_id"], "example-default")
        self.assertEqual(entries[0]["project_key"], "MAB")
        self.assertEqual(entries[0]["issue_keys"], ["MAB-1", "MAB-2"])

        state_module.store_seed_followup_context(
            session=session,
            tenant=tenant,
            request_id=request_id,
            user_id="u1",
            channel_ids=["c3"],
            project_id="example-default",
            project_key="MAB",
            issue_keys=["MAB-3"],
            questions=["q3"],
            prompt_markdown="updated",
        )
        entries = (tenant.discord_config or {}).get("seed_followups", [])
        self.assertEqual(entries[0]["channel_ids"], ["c3"])
        self.assertEqual(entries[0]["prompt_markdown"], "updated")

        found = state_module.find_seed_followup_context(tenant=tenant, channel_id="c3")
        self.assertIsNotNone(found)
        self.assertEqual(found["request_id"], request_id)
        self.assertIsNone(state_module.find_seed_followup_context(tenant=tenant, channel_id="missing"))

        with patch("orchestrator.api.discord.shared.state.save_seed_followups") as save_mock:
            state_module.clear_seed_followup_context(session=session, tenant=tenant, request_id=request_id)
        save_mock.assert_called_once()

    def test_tenant_seed_followups_normalization(self) -> None:
        tenant = SimpleNamespace(
            discord_config={
                "seed_followups": [
                    {"request_id": "", "prompt_markdown": "skip"},
                    {"request_id": "r1", "prompt_markdown": "", "channel_ids": ["c1"]},
                    {
                        "request_id": "r2",
                        "user_id": "u1",
                        "channel_ids": [" c1 ", ""],
                        "questions": [" q1 ", ""],
                        "issue_keys": ["mab-1", ""],
                        "project_id": "example-default",
                        "project_key": "mab",
                        "prompt_markdown": "prompt",
                    },
                ]
            }
        )

        entries = state_module.tenant_seed_followups(tenant)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["request_id"], "r2")
        self.assertEqual(entries[0]["channel_ids"], ["c1"])
        self.assertEqual(entries[0]["questions"], ["q1"])
        self.assertEqual(entries[0]["issue_keys"], ["MAB-1"])
        self.assertEqual(entries[0]["project_id"], "example-default")
        self.assertEqual(entries[0]["project_key"], "MAB")

    def test_find_seed_followup_context_skips_stale_entries(self) -> None:
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        tenant = SimpleNamespace(
            discord_config={
                "seed_followups": [
                    {
                        "request_id": "stale-1",
                        "channel_ids": ["c1"],
                        "issue_keys": ["MAB-1"],
                        "prompt_markdown": "stale",
                        "updated_at": stale_time,
                    }
                ]
            }
        )
        found = state_module.find_seed_followup_context(tenant=tenant, channel_id="c1")
        self.assertIsNone(found)

    def test_find_seed_followup_context_falls_back_to_unique_user_project_match(self) -> None:
        tenant = SimpleNamespace(
            discord_config={
                "seed_followups": [
                    {
                        "request_id": "r1",
                        "user_id": "u1",
                        "channel_ids": ["parent-c"],
                        "project_key": "GP",
                        "issue_keys": ["GP-1"],
                        "prompt_markdown": "prompt",
                    }
                ]
            }
        )
        found = state_module.find_seed_followup_context(
            tenant=tenant,
            channel_id="thread-c",
            user_id="u1",
            project_key="GP",
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["request_id"], "r1")

    def test_find_seed_followup_context_fallback_requires_unique_match(self) -> None:
        tenant = SimpleNamespace(
            discord_config={
                "seed_followups": [
                    {
                        "request_id": "r1",
                        "user_id": "u1",
                        "channel_ids": ["parent-c1"],
                        "project_key": "GP",
                        "issue_keys": ["GP-1"],
                        "prompt_markdown": "prompt-1",
                    },
                    {
                        "request_id": "r2",
                        "user_id": "u1",
                        "channel_ids": ["parent-c2"],
                        "project_key": "GP",
                        "issue_keys": ["GP-2"],
                        "prompt_markdown": "prompt-2",
                    },
                ]
            }
        )
        found = state_module.find_seed_followup_context(
            tenant=tenant,
            channel_id="thread-c",
            user_id="u1",
            project_key="GP",
        )
        self.assertIsNone(found)

    def test_remove_issue_key_from_seed_followups_prunes_entries(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(
            discord_config={
                "seed_followups": [
                    {
                        "request_id": "r1",
                        "channel_ids": ["c1"],
                        "issue_keys": ["MAB-1", "MAB-2"],
                        "prompt_markdown": "prompt",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    {
                        "request_id": "r2",
                        "channel_ids": ["c2"],
                        "issue_keys": ["MAB-1"],
                        "prompt_markdown": "prompt",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                ],
                "seed_followup_thread_channel_ids": ["c1", "c2"],
            },
            updated_at=None,
        )
        with patch("orchestrator.api.discord.shared.state.save_seed_followups") as save_mock:
            removed_contexts, removed_issue_refs = state_module.remove_issue_key_from_seed_followups(
                session=session,
                tenant=tenant,
                issue_key="MAB-1",
            )
        self.assertEqual((removed_contexts, removed_issue_refs), (1, 2))
        save_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
