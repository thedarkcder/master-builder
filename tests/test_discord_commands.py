from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from orchestrator.api.discord.ingress.executor import execute_discord_command
from orchestrator.api.discord.shared.state import store_seed_followup_context
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_state_machine import resolve_execution_gate_state
from orchestrator.core.decision_types import (
    DecisionClassification,
    IngressDecision,
)
from orchestrator.core.decision_engine import DecisionEngineResult
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.pre_run_check import PreRunCheckResult
from orchestrator.storage.models import Project, Run, Tenant, WorkflowExecution
from orchestrator.tools.jira_oauth import (
    JiraIssueDetail,
    JiraIssuePreview,
)
from tests.test_support.discord_command_api_harness import DiscordCommandApiTestHarness


pytestmark = pytest.mark.contract

class DiscordCommandApiTests(DiscordCommandApiTestHarness):
    def _queue_run(self, *, run_id: str, issue_key: str, status: str, project_id: str | None = None) -> None:
        with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            workflow_id = f"workflow-{run_id}"
            project_id = project_id or f"{self.tenant_id}-default"
            session.add(
                WorkflowExecution(
                    workflow_id=workflow_id,
                    tenant_id=self.tenant_id,
                    project_id=project_id,
                    issue_key=issue_key,
                    issue_summary=f"Issue {issue_key}",
                    issue_description="desc",
                    repo_url="https://github.com/example/repo",
                    branch=None,
                    pr_url=None,
                    dedupe_scope="issue_execution",
                    status=status,
                    last_error=None,
                    active_run_id=run_id,
                    latest_checkpoint_id=None,
                    source_workflow_id=None,
                    source_run_id=None,
                    blocked_reason=None,
                    created_at=now,
                    started_at=now if status == "running" else None,
                    finished_at=None if status in {"queued", "running"} else now,
                    updated_at=now,
                )
            )
            session.add(
                Run(
                    run_id=run_id,
                    workflow_id=workflow_id,
                    tenant_id=self.tenant_id,
                    project_id=project_id,
                    issue_key=issue_key,
                    issue_summary=f"Issue {issue_key}",
                    issue_description="desc",
                    repo_url="https://github.com/example/repo",
                    branch=None,
                    pr_url=None,
                    attempt_number=1,
                    parent_run_id=None,
                    entry_mode="fresh",
                    entry_stage="orchestrated",
                    entry_checkpoint_id=None,
                    dedupe_scope="issue_execution",
                    status=status,
                    last_error=None,
                    plan=None,
                    created_at=now,
                    started_at=now if status == "running" else None,
                    last_heartbeat_at=None,
                    worker_service_instance_id=None,
                    finished_at=None if status in {"queued", "running"} else now,
                )
            )
            session.commit()

    def _ready_precheck_result(self) -> PreRunCheckResult:
        return PreRunCheckResult(
            outcome="ready_for_agent",
            ready_label="agent:ready",
            ready_label_present=True,
            required_worker_capability="linux",
            required_worker_label="worker:linux",
            required_worker_label_present=True,
            decision_gate=DecisionGateResult(
                triggered=False,
                reason="Decision Gate not required",
                missing_sections=(),
                questions=(),
                recommendation="Proceed",
                tags=(),
            ),
            gtd=GoodToDoValidationResult(
                valid=True,
                missing_criteria=(),
                clarification_questions=(),
            ),
        )

    def _ready_decision_result(self) -> DecisionEngineResult:
        pre_check = self._ready_precheck_result()
        decision = IngressDecision(
            source="discord_run",
            pre_check=pre_check,
            block_reason=None,
            guidance=None,
            policy_error=None,
            label_actions=(),
        )
        return DecisionEngineResult(
            decision=decision,
            issue_labels=["agent:ready"],
            classification=DecisionClassification.CLEAR,
            missing_slots=[],
            auto_resolved_slots=[],
            case_id="case-ready",
            case_state="clear",
            cycle_id=None,
            outbox_effect_ids=(),
            duplicate_event=False,
            execution_gate=resolve_execution_gate_state(
                decision=decision,
                classification=DecisionClassification.CLEAR,
            ),
        )

    def _missing_ready_decision_result(self) -> DecisionEngineResult:
        pre_check = self._missing_ready_precheck_result()
        decision = IngressDecision(
            source="discord_run",
            pre_check=pre_check,
            block_reason="missing_ready_label",
            policy_error=None,
            guidance="Issue is missing the configured ready label. (agent:ready)",
            label_actions=(),
        )
        return DecisionEngineResult(
            decision=decision,
            issue_labels=[],
            classification=DecisionClassification.CLEAR,
            missing_slots=[],
            auto_resolved_slots=[],
            case_id="case-missing-ready",
            case_state="blocked",
            cycle_id=None,
            outbox_effect_ids=(),
            duplicate_event=False,
            execution_gate=resolve_execution_gate_state(
                decision=decision,
                classification=DecisionClassification.CLEAR,
            ),
        )

    def _missing_ready_precheck_result(self) -> PreRunCheckResult:
        return PreRunCheckResult(
            outcome="missing_ready_label",
            ready_label="agent:ready",
            ready_label_present=False,
            required_worker_capability="linux",
            required_worker_label="worker:linux",
            required_worker_label_present=True,
            decision_gate=DecisionGateResult(
                triggered=False,
                reason="Decision Gate not required",
                missing_sections=(),
                questions=(),
                recommendation="Proceed",
                tags=(),
            ),
            gtd=GoodToDoValidationResult(
                valid=True,
                missing_criteria=(),
                clarification_questions=(),
            ),
        )

    def _create_project(self, *, project_id: str, jira_project_key: str, channel_id: str) -> None:
        with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            session.add(
                Project(
                    project_id=project_id,
                    tenant_id=self.tenant_id,
                    name=project_id,
                    github_repository=f"https://github.com/example/{project_id}",
                    jira_project_key=jira_project_key,
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config={"channel_id": channel_id, "notify_events": []},
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def test_help_command_returns_supported_commands(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!help"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIn("!run", response.json()["message"])

    def test_status_command_includes_queue_and_active_runs(self) -> None:
        self._queue_run(run_id="run-queued-1", issue_key="TP-10", status="queued")
        self._queue_run(run_id="run-running-1", issue_key="TP-11", status="running")

        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!status"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["data"]["queue_depth"], 1)
        self.assertEqual(len(response.json()["data"]["active_runs"]), 1)

    def test_sensitive_commands_require_allowlisted_user(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!run TP-99"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("allowlisted", response.json()["detail"])

    def test_thread_channel_is_allowed_when_registered_for_project(self) -> None:
        with self.session_factory() as session:
            project = session.get(Project, f"{self.tenant_id}-default")
            self.assertIsNotNone(project)
            discord_config = dict(project.discord_config or {})
            discord_config["ask_thread_channel_ids"] = ["discord-thread-1"]
            project.discord_config = discord_config
            session.commit()

        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-thread-1", "command": "!help"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_non_registered_channel_is_rejected(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-other-1", "command": "!help"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("does not match", response.json()["detail"])

    def test_cancel_marks_run_as_cancelled(self) -> None:
        self._queue_run(run_id="run-cancel-me", issue_key="TP-12", status="queued")

        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!cancel run-cancel-me"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], "cancelled")

    def test_run_enqueues_when_jira_issue_is_executable(self) -> None:
        with patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-20", summary="Do thing", status="To Do"),
        ), patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_detail",
            return_value=JiraIssueDetail(
                key="TP-20",
                summary="Do thing",
                status="To Do",
                description="Objective: run command should carry Jira detail context.",
            ),
        ), patch(
            "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.evaluate_issue_clarification_state",
            return_value=self._ready_decision_result(),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!run TP-20"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["data"]["issue_key"], "TP-20")
        run_id = response.json()["data"]["run_id"]
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            assert run is not None
            self.assertEqual(run.issue_description, "Objective: run command should carry Jira detail context.")

    def test_run_precheck_uses_canonical_issue_description_without_board_context(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
                return_value=JiraIssuePreview(key="TP-20", summary="Do thing", status="To Do"),
            ),
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_detail",
                return_value=JiraIssueDetail(
                    key="TP-20",
                    summary="Do thing",
                    status="To Do",
                    description="Objective: run command should carry Jira detail context.",
                    labels=["agent:ready", "worker:linux"],
                ),
            ),
            patch(
                "orchestrator.api.discord.ingress.ask_runtime._search_jira_issues_for_tenant",
                return_value=[
                    JiraIssuePreview(key="TP-20", summary="Do thing", status="To Do"),
                    JiraIssuePreview(key="TP-21", summary="Sibling ticket A", status="To Do"),
                    JiraIssuePreview(key="TP-22", summary="Sibling ticket B", status="Testing"),
                ],
            ),
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.evaluate_issue_clarification_state",
                return_value=self._ready_decision_result(),
            ) as decision_mock,
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!run TP-20"},
            )

        self.assertEqual(response.status_code, 200)
        issue_description = str(decision_mock.call_args.kwargs["event"].issue_description)
        self.assertIn("agent:ready", decision_mock.call_args.kwargs["event"].issue_labels)
        self.assertEqual(issue_description, "Objective: run command should carry Jira detail context.")

    def test_run_conflict_includes_active_run_details(self) -> None:
        self._queue_run(run_id="run-active-1", issue_key="TP-20", status="running")
        with patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-20", summary="Do thing", status="To Do"),
        ), patch(
            "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.evaluate_issue_clarification_state",
            return_value=self._ready_decision_result(),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!run TP-20"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("run_already_active", response.json()["detail"])
        self.assertIn("run-active-1", response.json()["detail"])
        self.assertIn("running", response.json()["detail"])

    def test_retry_enqueues_from_latest_failed_run(self) -> None:
        self._queue_run(run_id="run-failed-1", issue_key="TP-30", status="failed")
        with patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-30", summary="Retry thing", status="To Do"),
        ), patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_detail",
            return_value=JiraIssueDetail(
                key="TP-30",
                summary="Retry thing",
                status="To Do",
                description="Objective: refreshed from Jira for retry.",
            ),
        ), patch(
            "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.evaluate_issue_clarification_state",
            return_value=self._ready_decision_result(),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!retry run-failed-1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["data"]["issue_key"], "TP-30")
        retry_run_id = response.json()["data"]["run_id"]
        with self.session_factory() as session:
            retry_run = session.get(Run, retry_run_id)
            assert retry_run is not None
            self.assertEqual(retry_run.issue_description, "Objective: refreshed from Jira for retry.")

    def test_retry_allows_in_progress_issue_status(self) -> None:
        self._queue_run(run_id="run-failed-2", issue_key="TP-31", status="failed")
        with patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-31", summary="Retry thing", status="In Progress"),
        ), patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_detail",
            return_value=JiraIssueDetail(
                key="TP-31",
                summary="Retry thing",
                status="In Progress",
                description="Objective: refreshed from Jira for retry.",
            ),
        ), patch(
            "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.evaluate_issue_clarification_state",
            return_value=self._ready_decision_result(),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!retry run-failed-2"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["data"]["issue_key"], "TP-31")

    def test_retry_conflict_includes_active_run_details(self) -> None:
        self._queue_run(run_id="run-failed-1", issue_key="TP-30", status="failed")
        self._queue_run(run_id="run-active-2", issue_key="TP-30", status="queued")
        with patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-30", summary="Retry thing", status="To Do"),
        ), patch(
            "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.evaluate_issue_clarification_state",
            return_value=self._ready_decision_result(),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!retry run-failed-1"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("run_already_active", response.json()["detail"])
        self.assertIn("run-active-2", response.json()["detail"])

    def test_link_rejects_issue_outside_mapped_project_scope(self) -> None:
        self._create_project(project_id=f"{self.tenant_id}-other", jira_project_key="OTH", channel_id="discord-other-1")
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-other-1", "command": "!link TP-20"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("outside the mapped project scope", response.json()["detail"])

    def test_run_rejects_issue_outside_mapped_project_scope(self) -> None:
        other_project_id = f"{self.tenant_id}-other"
        self._create_project(project_id=other_project_id, jira_project_key="OTH", channel_id="discord-other-1")
        self._set_project_allowed_users(project_id=other_project_id, user_ids=["u-admin"])
        with patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-20", summary="Do thing", status="To Do"),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-other-1", "command": "!run TP-20"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertIn("outside the mapped project scope", response.json()["detail"])

    def test_retry_rejects_run_outside_mapped_project_scope(self) -> None:
        other_project_id = f"{self.tenant_id}-other"
        self._create_project(project_id=other_project_id, jira_project_key="OTH", channel_id="discord-other-1")
        self._set_project_allowed_users(project_id=other_project_id, user_ids=["u-admin"])
        self._queue_run(run_id="run-tp-1", issue_key="TP-30", status="failed", project_id=f"{self.tenant_id}-default")
        with patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-30", summary="Retry thing", status="To Do"),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-other-1", "command": "!retry run-tp-1"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertIn("outside the mapped project scope", response.json()["detail"])

    def test_cancel_rejects_run_outside_mapped_project_scope(self) -> None:
        other_project_id = f"{self.tenant_id}-other"
        self._create_project(project_id=other_project_id, jira_project_key="OTH", channel_id="discord-other-1")
        self._set_project_allowed_users(project_id=other_project_id, user_ids=["u-admin"])
        self._queue_run(run_id="run-tp-cancel", issue_key="TP-31", status="queued", project_id=f"{self.tenant_id}-default")
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-admin", "channel_id": "discord-other-1", "command": "!cancel run-tp-cancel"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("outside the mapped project scope", response.json()["detail"])

    def test_archived_project_channel_is_rejected(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Archived Project",
                "github_repository": "https://github.com/example/archived",
                "jira_project_key": "ARC",
                "discord": {"channel_id": "discord-archived-project", "notify_events": []},
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        project_id = create_project.json()["project_id"]
        archive_response = self.client.put(
            f"/api/admin/tenants/{self.tenant_id}/projects/{project_id}",
            json={
                "name": "Archived Project",
                "github_repository": "https://github.com/example/archived",
                "jira_project_key": "ARC",
                "policy_overrides": {},
                "environment": {},
                "secret_refs": {},
                "discord": {"channel_id": "discord-archived-project", "notify_events": []},
                "is_archived": True,
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(archive_response.status_code, 200)

        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-archived-project", "command": "!help"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("does not match", response.json()["detail"])

    def test_status_and_runs_are_scoped_to_project_channel(self) -> None:
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
        other_project_id = create_project.json()["project_id"]

        self._queue_run(run_id="run-default-queued", issue_key="TP-10", status="queued")
        self._queue_run(run_id="run-default-running", issue_key="TP-11", status="running")
        self._queue_run(run_id="run-other-queued", issue_key="OTH-10", status="queued", project_id=other_project_id)
        self._queue_run(run_id="run-other-running", issue_key="OTH-11", status="running", project_id=other_project_id)

        status_response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-2", "command": "!status"},
        )
        self.assertEqual(status_response.status_code, 200)
        self.assertNotIn("webhook_last_seen", status_response.json()["data"])
        self.assertEqual(status_response.json()["data"]["queue_depth"], 1)
        self.assertEqual(len(status_response.json()["data"]["active_runs"]), 1)
        self.assertEqual(status_response.json()["data"]["active_runs"][0]["issue_key"], "OTH-11")

        runs_response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-2", "command": "!runs 10"},
        )
        self.assertEqual(runs_response.status_code, 200)
        run_issue_keys = [entry["issue_key"] for entry in runs_response.json()["data"]["runs"]]
        self.assertIn("OTH-10", run_issue_keys)
        self.assertIn("OTH-11", run_issue_keys)
        self.assertNotIn("TP-10", run_issue_keys)
        self.assertNotIn("TP-11", run_issue_keys)

    def test_status_and_runs_with_dm_channel_use_tenant_scope(self) -> None:
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
        other_project_id = create_project.json()["project_id"]

        self._queue_run(run_id="run-default-queued", issue_key="TP-10", status="queued")
        self._queue_run(run_id="run-other-running", issue_key="OTH-11", status="running", project_id=other_project_id)

        status_response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": None, "command": "!status"},
        )
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["data"]["queue_depth"], 1)
        self.assertEqual(len(status_response.json()["data"]["active_runs"]), 1)
        self.assertEqual(status_response.json()["data"]["active_runs"][0]["issue_key"], "OTH-11")

        runs_response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": None, "command": "!runs 10"},
        )
        self.assertEqual(runs_response.status_code, 200)
        run_issue_keys = {entry["issue_key"] for entry in runs_response.json()["data"]["runs"]}
        self.assertIn("TP-10", run_issue_keys)
        self.assertIn("OTH-11", run_issue_keys)

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
                return_value=[JiraIssuePreview(key="OTH-50", summary="Scoped issue", status="To Do")],
            ) as search_mock,
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch("orchestrator.api.discord.commands.ask.answer_board_question_with_runtime", return_value="Scoped answer"),
        ):
            mapped = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-viewer", "channel_id": "discord-channel-2", "command": "!ask scoped"},
            )
            self.assertEqual(mapped.status_code, 200)
            mapped_jql = str(search_mock.call_args.kwargs["jql"])
            self.assertIn('project = "OTH"', mapped_jql)

            dm = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-viewer", "channel_id": None, "command": "!ask unscoped"},
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
            self.assertIn("requires a single mapped project scope", str(jira_ctx.exception.detail))

        unmapped = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-unmapped", "command": "!ask blocked"},
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
                scoped_issue_key.strip().upper() if isinstance(scoped_issue_key, str) and scoped_issue_key.strip() else None,
                None,
                [{"key": "TP-77", "summary": "Investigate", "status": "To Do"}],
                {"To Do": 1},
                [],
            )

        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.ask_history_runtime.existing_issue_keys_for_tenant", return_value=set()),
            patch("orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context", side_effect=_collect_stub),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch("orchestrator.api.discord.commands.ask.answer_board_question_with_runtime", return_value="Board answer"),
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
                if entry.get("user_id") == "u-viewer" and entry.get("channel_id") == "discord-channel-1"
            ]
        self.assertFalse(
            any(str(entry.get("issue_key") or "").strip().upper() == "TP-404" for entry in history_entries)
        )

    def test_plain_text_is_treated_as_implicit_ask_when_enabled(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [], {"Blocked": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
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
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!gap"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !gap", response.json()["detail"])

    def test_gap_command_returns_analysis(self) -> None:
        with patch(
            "orchestrator.api.discord.ingress.gap_runtime.run_gap_analysis",
            return_value=(
                "Gap analysis for [TP-77](https://example.atlassian.net/browse/TP-77)",
                {
                    "issue_key": "TP-77",
                    "jira_url": "https://example.atlassian.net/browse/TP-77",
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
                "orchestrator.api.discord.ingress.seed_runtime.seed_parent_issues_with_runtime",
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
                "orchestrator.api.discord.ingress.seed_runtime.seed_parent_issues_with_runtime",
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
