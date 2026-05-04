from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestrator.runtime.issue_fanout import build_seed_issue_description, seed_issues_with_runtime
from orchestrator.core.runtime.payload_models import EngineeringSeedPlanPayload, PmParentSeedPlanPayload
from orchestrator.storage.models import AtlassianOAuthConnection, Tenant
from orchestrator.tools.atlassian_oauth import JiraIssueCreateResult, JiraIssuePreview
from tests.test_support.discord_command_api_harness import DiscordCommandApiTestHarness


pytestmark = pytest.mark.contract


class DiscordSeedCommandFlowTests(DiscordCommandApiTestHarness):
    def test_issues_seed_requires_spec(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!issues seed"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !issues seed", response.json()["detail"])

    def test_issues_seed_calls_codex_seed_flow(self) -> None:
        with patch(
            "orchestrator.runtime.issue_fanout.seed_parent_issues_with_runtime",
            return_value=(
                "PM parent issue upsert complete. Created 2: TP-1, TP-2. Updated 0: none.",
                {"created_parent_issue_keys": ["TP-1", "TP-2"]},
            ),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-admin",
                    "channel_id": "discord-channel-1",
                    "command": "!issues seed Build API and webhook tasks",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["command"], "issues")
        self.assertIn("TP-1", response.json()["message"])

    def test_issues_seed_with_incomplete_oauth_context_returns_controlled_502(self) -> None:
        with (
            patch("orchestrator.runtime.issue_fanout.build_issue_seed_runtime", return_value=object()),
            patch(
                "orchestrator.runtime.issue_fanout.plan_pm_parent_issues_with_runtime",
                return_value=PmParentSeedPlanPayload.from_payload(
                    {
                        "project_key": "TP",
                        "issues": [
                            {
                                "summary": "Build API and webhook reliability feature",
                                "issue_type": "Story",
                                "objective": "Improve reliability",
                                "user_value": "Customers see fewer delivery failures",
                                "recommendation": "Ship API validation plus webhook retries",
                                "scope_in": ["API changes"],
                                "scope_out": [],
                                "acceptance_criteria": ["Validation passes"],
                                "ui_references": [],
                                "dependencies": [],
                                "risks": [],
                                "open_questions": [],
                                "success_outcomes": ["Lower webhook failure rate"],
                                "labels": [],
                            },
                        ],
                        "questions": [],
                    }
                ),
            ),
            patch(
                "orchestrator.runtime.issue_fanout.tenant_atlassian_oauth_context",
                return_value={"access_token": "tok-only"},
            ),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-admin",
                    "channel_id": "discord-channel-1",
                    "command": "!issues seed Build API and webhook tasks",
                },
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("Failed to seed Jira issues", response.json()["detail"])
        self.assertIn("Atlassian context is incomplete", response.json()["detail"])
        self.assertNotIn("tok-only", response.json()["detail"])
        self.assertNotIn("Internal server error. Ref:", response.json()["detail"])

    def test_seed_issues_requests_clarifications_when_required_fields_missing(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            jira_config = dict(tenant.jira_config)
            jira_config["connection_id"] = "conn-seed-clarify"
            tenant.jira_config = jira_config
            session.add(
                AtlassianOAuthConnection(
                    connection_id="conn-seed-clarify",
                    account_id="acct-1",
                    account_email="dev@example.com",
                    cloud_id="cloud-1",
                    site_url="https://example.atlassian.net",
                    scopes=["read:jira-work", "write:jira-work"],
                    access_token_encrypted="enc",
                    refresh_token_encrypted="enc",
                    access_token_expires_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        class _FakeClient:
            def list_project_issue_types_for_create(self, **_kwargs: object) -> list[str]:
                return ["Story", "Sub-task", "Task"]

            def search_issues_by_jql(
                self,
                *,
                access_token: str,
                cloud_id: str,
                jql: str,
                max_results: int = 20,
                start_at: int = 0,
            ) -> list[JiraIssuePreview]:
                return []

            def create_issue(
                self,
                *,
                access_token: str,
                cloud_id: str,
                project_key: str,
                issue,
            ) -> JiraIssueCreateResult:
                if getattr(issue, "parent_issue_key", None):
                    return JiraIssueCreateResult(key="TP-301", issue_id="301")
                return JiraIssueCreateResult(key="TP-300", issue_id="300")

            def update_issue_summary(
                self,
                *,
                access_token: str,
                cloud_id: str,
                issue_id_or_key: str,
                summary: str,
            ) -> None:
                return None

            def replace_issue_labels(
                self,
                *,
                access_token: str,
                cloud_id: str,
                issue_id_or_key: str,
                labels: list[str],
            ) -> None:
                return None

            def add_issue_link(
                self,
                *,
                access_token: str,
                cloud_id: str,
                inward_issue_key: str,
                outward_issue_key: str,
                link_type: str = "Relates",
            ) -> dict:
                return {}

        with (
            self.session_factory() as session,
            patch("orchestrator.runtime.issue_fanout.build_issue_seed_runtime", return_value=object()),
            patch(
                "orchestrator.runtime.issue_fanout.plan_seed_issues_with_runtime",
                return_value=EngineeringSeedPlanPayload.from_payload(
                    {
                        "project_key": "TP",
                        "parent_issue": {
                        "summary": "Improve worker retry reliability",
                        "issue_type": "Story",
                        "objective": "Improve reliability",
                        "user_value": "Operators see fewer worker failures",
                        "recommendation": "Ship bounded retries first",
                        "scope_in": ["Worker retry strategy"],
                        "scope_out": ["UI changes"],
                        "acceptance_criteria": ["Retries are bounded and observable"],
                        "ui_references": [],
                        "dependencies": [],
                        "risks": [],
                        "open_questions": [],
                        "success_outcomes": ["Lower worker retry failures"],
                        "labels": ["seeded"],
                        },
                        "questions": ["What is the rollout plan?"],
                        "engineering_children": [
                            {
                            "summary": "Create worker retries",
                            "issue_type": "Sub-task",
                            "capability": "Worker retry safety",
                            "delivery": "Build bounded worker retries so failed jobs can be retried safely.",
                            "expected_outcome": "Failed worker jobs retry within controlled limits and remain observable.",
                            "acceptance_criteria": ["Retries are bounded and observable"],
                            "dependencies": [],
                            "risks": [],
                            "how_to_test": ["Run worker retry integration test"],
                            "done_means": ["Retries are bounded and observable"],
                            "labels": ["seeded"],
                            },
                        ],
                    }
                ),
            ),
            patch(
                "orchestrator.runtime.issue_fanout.tenant_atlassian_oauth_context",
                return_value={
                    "client": _FakeClient(),
                    "access_token": "token",
                    "connection": type("Connection", (), {"cloud_id": "cloud-1", "site_url": "https://example.atlassian.net"})(),
                },
            ),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            message, data = seed_issues_with_runtime(
                session=session,
                tenant=tenant,
                prompt_markdown="Seed issues from spec",
                scoped_project_id=self.default_project_id,
            )

        self.assertIn("need more detail", message.lower())
        self.assertTrue(data["requires_input"])
        self.assertEqual(data["created_issue_keys"], ["TP-300", "TP-301"])
        self.assertEqual(data["questions"], ["What is the rollout plan?"])
        self.assertEqual(data["children_sync_status"], "sync_blocked")

    def test_seed_issues_updates_matching_existing_issue(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            jira_config = dict(tenant.jira_config)
            jira_config["connection_id"] = "conn-seed-upsert"
            tenant.jira_config = jira_config
            session.add(
                AtlassianOAuthConnection(
                    connection_id="conn-seed-upsert",
                    account_id="acct-1",
                    account_email="dev@example.com",
                    cloud_id="cloud-1",
                    site_url="https://example.atlassian.net",
                    scopes=["read:jira-work", "write:jira-work"],
                    access_token_encrypted="enc",
                    refresh_token_encrypted="enc",
                    access_token_expires_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        class _FakeClient:
            def __init__(self) -> None:
                self.updated_issue_summary_keys: list[str] = []
                self.updated_issue_field_keys: list[str] = []
                self.replaced_label_keys: list[str] = []
                self.create_called = False

            def list_project_issue_types_for_create(self, **_kwargs: object) -> list[str]:
                return ["Story", "Sub-task", "Task"]

            def search_issues_by_jql(
                self,
                *,
                access_token: str,
                cloud_id: str,
                jql: str,
                max_results: int = 20,
                start_at: int = 0,
            ) -> list[JiraIssuePreview]:
                return [
                    JiraIssuePreview(key="TP-110", summary="Improve worker retry reliability", status="To Do"),
                    JiraIssuePreview(key="TP-111", summary="Create worker retries", status="To Do"),
                ]

            def get_issue_detail(
                self,
                *,
                access_token: str,
                cloud_id: str,
                issue_id_or_key: str,
            ) -> SimpleNamespace:
                return SimpleNamespace(
                    key=str(issue_id_or_key),
                    summary="Improve worker retry reliability",
                    status="To Do",
                    issue_type="Story",
                )

            def update_issue_summary(
                self,
                *,
                access_token: str,
                cloud_id: str,
                issue_id_or_key: str,
                summary: str,
            ) -> None:
                self.updated_issue_summary_keys.append(str(issue_id_or_key))

            def update_issue_fields(
                self,
                *,
                access_token: str,
                cloud_id: str,
                issue_id_or_key: str,
                summary: str,
                description,
                labels: list[str],
            ) -> None:
                self.updated_issue_field_keys.append(str(issue_id_or_key))

            def replace_issue_labels(
                self,
                *,
                access_token: str,
                cloud_id: str,
                issue_id_or_key: str,
                labels: list[str],
            ) -> None:
                self.replaced_label_keys.append(str(issue_id_or_key))

            def create_issue(
                self,
                *,
                access_token: str,
                cloud_id: str,
                project_key: str,
                issue,
            ) -> JiraIssueCreateResult:
                self.create_called = True
                return JiraIssueCreateResult(key="TP-999", issue_id="999")

            def add_issue_link(
                self,
                *,
                access_token: str,
                cloud_id: str,
                inward_issue_key: str,
                outward_issue_key: str,
                link_type: str = "Relates",
            ) -> dict:
                return {}

        fake_client = _FakeClient()
        with (
            self.session_factory() as session,
            patch("orchestrator.runtime.issue_fanout.build_issue_seed_runtime", return_value=object()),
            patch(
                "orchestrator.runtime.issue_fanout.plan_seed_issues_with_runtime",
                return_value=EngineeringSeedPlanPayload.from_payload(
                    {
                        "project_key": "TP",
                        "parent_issue": {
                        "summary": "Improve worker retry reliability",
                        "issue_type": "Story",
                        "objective": "Improve reliability",
                        "user_value": "Operators see fewer worker failures",
                        "recommendation": "Ship bounded retries first",
                        "scope_in": ["Worker retry strategy"],
                        "scope_out": ["UI changes"],
                        "acceptance_criteria": ["Retries are bounded and observable"],
                        "ui_references": [],
                        "dependencies": [],
                        "risks": [],
                        "open_questions": [],
                        "success_outcomes": ["Lower worker retry failures"],
                        "labels": ["seeded"],
                        },
                        "engineering_children": [
                            {
                            "summary": "Create worker retries",
                            "issue_type": "Sub-task",
                            "capability": "Worker retry safety",
                            "delivery": "Build bounded worker retries so failed jobs can be retried safely.",
                            "expected_outcome": "Failed worker jobs retry within controlled limits and remain observable.",
                            "acceptance_criteria": ["Retries are bounded and observable"],
                            "dependencies": [],
                            "risks": [],
                            "how_to_test": ["Run worker retry integration test"],
                            "done_means": ["Retries are bounded and observable"],
                            "labels": ["seeded"],
                            },
                        ],
                        "questions": [],
                    }
                ),
            ),
            patch(
                "orchestrator.runtime.issue_fanout.tenant_atlassian_oauth_context",
                return_value={
                    "client": fake_client,
                    "access_token": "token",
                    "connection": type("Connection", (), {"cloud_id": "cloud-1", "site_url": "https://example.atlassian.net"})(),
                },
            ),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            message, data = seed_issues_with_runtime(
                session=session,
                tenant=tenant,
                prompt_markdown="Seed issues from spec",
                scoped_project_id=self.default_project_id,
            )

        self.assertIn("Updated 2", message)
        self.assertEqual(data["updated_issue_keys"], ["TP-110", "TP-111"])
        self.assertEqual(data["created_issue_keys"], [])
        self.assertEqual(fake_client.updated_issue_summary_keys, ["TP-110", "TP-110"])
        self.assertEqual(fake_client.updated_issue_field_keys, ["TP-111"])
        self.assertEqual(fake_client.replaced_label_keys, ["TP-110", "TP-110"])
        self.assertFalse(fake_client.create_called)

    def test_seed_issue_description_is_native_jira_adf(self) -> None:
        description = build_seed_issue_description(
            objective="Ship feature",
            scope_in=["API endpoint"],
            scope_out=["Mobile app changes"],
            acceptance_criteria=["Endpoint returns 200"],
            how_to_test=["Run API integration tests"],
            nfr_intent="MVP",
            dependencies_and_risks=["Depends on staging API availability"],
        )
        self.assertEqual(description.get("type"), "doc")
        content = description.get("content", [])
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "heading")
        self.assertEqual(content[0]["content"][0]["text"], "Capability")
        self.assertEqual(content[1]["type"], "bulletList")
        first_bullet = content[1]["content"][0]["content"][0]["content"][0]["text"]
        self.assertEqual(first_bullet, "Ship feature")
        heading_texts = [node["content"][0]["text"] for node in content if node.get("type") == "heading"]
        self.assertIn("What to Build", heading_texts)
        self.assertIn("How to Test", heading_texts)
        self.assertIn("Synced From Parent Revision", heading_texts)
