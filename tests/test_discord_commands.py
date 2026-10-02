from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from orchestrator.api.discord.ingress.executor import execute_discord_command
from orchestrator.api.discord.shared.state import store_seed_followup_context
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.runtime.payload_models import AskIntent
from orchestrator.storage.models import Tenant
from orchestrator.tools.atlassian_oauth import JiraIssuePreview
from tests.test_support.discord_command_api_harness import DiscordCommandApiTestHarness


pytestmark = pytest.mark.contract


class DiscordCommandApiTests(DiscordCommandApiTestHarness):
    def test_ask_ingress_scope_contract_matrix(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Other Project",
                "github_repository": "https://github.com/example/other",
                "jira_project_key": "OTH",
                "discord": {"channel_id": "discord-channel-2", "notify_events": []},
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)

        with (
            patch(
                "orchestrator.api.discord.ingress.ask_runtime._search_jira_issues_for_tenant",
                return_value=[
                    JiraIssuePreview(
                        key="OTH-50", summary="Scoped issue", status="To Do"
                    )
                ],
            ) as search_mock,
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_runtime",
                return_value=AskIntent(mode="answer", summary="answer", command=None),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_runtime",
                return_value="Scoped answer",
            ),
        ):
            mapped = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-viewer",
                    "channel_id": "discord-channel-2",
                    "command": "!ask scoped",
                },
            )
            self.assertEqual(mapped.status_code, 200)
            mapped_jql = str(search_mock.call_args.kwargs["jql"])
            self.assertIn('project = "OTH"', mapped_jql)

            dm = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-viewer",
                    "channel_id": None,
                    "command": "!ask unscoped",
                },
            )
            self.assertEqual(dm.status_code, 409)
            self.assertIn("requires a single mapped project scope", dm.json()["detail"])

            with self.session_factory() as session:
                with self.assertRaises(HTTPException) as jira_ctx:
                    execute_discord_command(
                        tenant_id=self.tenant_id,
                        payload=DiscordCommandRequest(
                            user_id="jira-user-1",
                            channel_id=None,
                            command="!ask via-jira",
                        ),
                        session=session,
                        ingress_source="jira_comment",
                    )
            self.assertEqual(jira_ctx.exception.status_code, 409)
            self.assertIn(
                "requires a single mapped project scope", str(jira_ctx.exception.detail)
            )

        unmapped = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={
                "user_id": "u-viewer",
                "channel_id": "discord-unmapped",
                "command": "!ask blocked",
            },
        )
        self.assertEqual(unmapped.status_code, 403)
        self.assertIn("does not match", unmapped.json()["detail"])

    def test_ask_follow_up_drops_deleted_history_issue_key(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            discord_config = dict(tenant.discord_config or {})
            discord_config["ask_history"] = [
                {
                    "user_id": "u-viewer",
                    "channel_id": "discord-channel-1",
                    "question": "What changed?",
                    "answer": "Previous answer",
                    "issue_key": "TP-404",
                    "status": None,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
            tenant.discord_config = discord_config
            jira_config = dict(tenant.jira_config or {})
            jira_config["connection_id"] = "connection-for-prune-test"
            tenant.jira_config = jira_config
            session.commit()

        collect_calls: list[str | None] = []

        def _collect_stub(*, scoped_issue_key, **_kwargs):  # type: ignore[no-untyped-def]
            collect_calls.append(scoped_issue_key)
            return (
                scoped_issue_key.strip().upper()
                if isinstance(scoped_issue_key, str) and scoped_issue_key.strip()
                else None,
                None,
                [{"key": "TP-77", "summary": "Investigate", "status": "To Do"}],
                {"To Do": 1},
                [],
            )

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_history_runtime.existing_issue_keys_for_tenant",
                return_value=set(),
            ),
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                side_effect=_collect_stub,
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_runtime",
                return_value=AskIntent(mode="answer", summary="answer", command=None),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_runtime",
                return_value="Board answer",
            ),
        ):
            response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!ask what changed since last update?",
                ),
                session=session,
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.message, "Board answer")
        self.assertEqual(collect_calls, [None])

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            history_entries = [
                entry
                for entry in (tenant.discord_config or {}).get("ask_history", [])
                if entry.get("user_id") == "u-viewer"
                and entry.get("channel_id") == "discord-channel-1"
            ]
        self.assertFalse(
            any(
                str(entry.get("issue_key") or "").strip().upper() == "TP-404"
                for entry in history_entries
            )
        )

    def test_plain_text_is_treated_as_implicit_ask_when_enabled(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [], {"Blocked": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_runtime",
                return_value=AskIntent(mode="answer", summary="answer", command=None),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_runtime",
                return_value="Implicit ask answer",
            ),
        ):
            response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="what is blocked on this board?",
                ),
                session=session,
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.command, "ask")
        self.assertEqual(response.message, "Implicit ask answer")

    def test_gap_command_requires_issue_key(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={
                "user_id": "u-viewer",
                "channel_id": "discord-channel-1",
                "command": "!gap",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !gap", response.json()["detail"])

    def test_gap_command_returns_analysis(self) -> None:
        with patch(
            "orchestrator.api.discord.ingress.gap_runtime.run_gap_analysis",
            return_value=(
                "Gap analysis for [TP-77](https://example-2.atlassian.net/browse/TP-77)",
                {
                    "issue_key": "TP-77",
                    "jira_url": "https://example-2.atlassian.net/browse/TP-77",
                    "pr_url": "https://github.com/example/repo/pull/12",
                    "confidence": "medium",
                },
            ),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-viewer",
                    "channel_id": "discord-channel-1",
                    "command": "!gap TP-77",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["command"], "gap")
        self.assertEqual(response.json()["data"]["issue_key"], "TP-77")
        self.assertIn("TP-77", response.json()["message"])

    def test_plain_text_in_seed_followup_thread_routes_to_issues_followup(self) -> None:
        self._set_project_allowed_users(
            project_id=self.default_project_id,
            user_ids=["u-admin", "u-viewer"],
        )
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            store_seed_followup_context(
                session=session,
                tenant=tenant,
                request_id="followup-1",
                user_id="u-viewer",
                channel_ids=["discord-channel-1"],
                project_id=self.default_project_id,
                project_key="TP",
                issue_keys=["TP-11"],
                questions=["What is the rollout plan?"],
                prompt_markdown="Original seed prompt",
            )
            session.commit()

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.runtime.issue_fanout.seed_parent_issues_with_runtime",
                return_value=(
                    "PM parent issue upsert complete. Updated 1: TP-11. Created 0: none.",
                    {
                        "requires_input": False,
                        "project_key": "TP",
                        "questions": [],
                        "all_parent_issue_keys": ["TP-11"],
                    },
                ),
            ) as seed_mock,
        ):
            response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="Here are the missing rollout details",
                ),
                session=session,
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.command, "issues")
        self.assertIn("PM parent issue upsert complete", response.message)
        seed_mock.assert_called_once()
        kwargs = seed_mock.call_args.kwargs
        self.assertEqual(kwargs["allow_create"], False)
        self.assertEqual(kwargs["force_issue_keys"], ["TP-11"])
        self.assertIn("Here are the missing rollout details", kwargs["prompt_markdown"])

    def test_plain_text_in_seed_followup_thread_beats_plain_ask_routing(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            store_seed_followup_context(
                session=session,
                tenant=tenant,
                request_id="followup-plain-1",
                user_id="u-viewer",
                channel_ids=["discord-channel-1"],
                project_id=self.default_project_id,
                project_key="TP",
                issue_keys=["TP-11"],
                questions=["What is the rollout plan?"],
                prompt_markdown="Original seed prompt",
            )
            session.commit()

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.runtime.issue_fanout.seed_parent_issues_with_runtime",
                return_value=(
                    "PM parent issue upsert complete. Updated 1: TP-11. Created 0: none.",
                    {
                        "requires_input": False,
                        "project_key": "TP",
                        "questions": [],
                        "all_parent_issue_keys": ["TP-11"],
                    },
                ),
            ) as seed_mock,
        ):
            response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="More rollout details",
                ),
                session=session,
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.command, "issues")
        seed_mock.assert_called_once()

    def test_execute_discord_command_rejects_unknown_ingress_source(self) -> None:
        with self.session_factory() as session:
            with self.assertRaises(HTTPException) as exc:
                execute_discord_command(
                    tenant_id=self.tenant_id,
                    payload=DiscordCommandRequest(
                        user_id="u-admin",
                        channel_id="discord-channel-1",
                        command="!help",
                    ),
                    session=session,
                    ingress_source="slack",  # type: ignore[arg-type]
                )
        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn("Unsupported ingress source", str(exc.exception.detail))
