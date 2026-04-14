import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from orchestrator.api.discord.bug.service import build_discord_bug_description
from orchestrator.api.discord.ask.context import project_filter_jql
from orchestrator.api.discord.ingress.ask_runtime import ask_board_message, collect_ask_context, collect_github_ask_context
from orchestrator.api.discord.ingress.bug_runtime import create_discord_bug_issue
from orchestrator.api.discord.ingress.executor import execute_discord_command
from orchestrator.api.discord.shared.state import store_seed_followup_context
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.config import get_settings
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_state_machine import resolve_execution_gate_state
from orchestrator.core.decision_types import (
    DecisionClassification,
    IngressDecision,
)
from orchestrator.core.decision_engine import DecisionEngineResult
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.pre_run_check import PreRunCheckResult
from orchestrator.storage.models import JiraOAuthConnection, Project, Run, Tenant, WorkflowExecution
from orchestrator.tools.github_app import GitHubApiError
from orchestrator.tools.jira_oauth import (
    JiraIssueBulkCreateResult,
    JiraIssueCreateResult,
    JiraIssueDetail,
    JiraIssuePreview,
    JiraOAuthError,
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

    def test_ask_command_requires_question(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!ask"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !ask", response.json()["detail"])

    def test_pm_command_requires_question(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!pm"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !pm", response.json()["detail"])

    def test_pm_command_returns_product_first_answer_and_stores_minimal_history(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_pm_interview_with_codex",
                return_value={
                    "message": (
                        "What kind of rollout narrative do you need?\n"
                        "Examples: executive launch summary, customer-facing changelog, or support handoff."
                    ),
                    "brief": {
                        "objective": "Improve checkout recovery",
                        "user_value": "Customers recover from checkout failures more clearly.",
                        "target_user": "",
                        "primary_journey": "",
                        "acceptance_criteria": [],
                        "ui_references": [],
                        "constraints": [],
                        "success_outcomes": [],
                        "recommendation": "Ship in one sprint",
                        "scope_in": ["Retry flow"],
                        "scope_out": ["Payments provider migration"],
                        "risks": ["Missing telemetry"],
                        "open_questions": ["Fallback copy approval"],
                        "next_steps": ["Clarify the intended rollout audience."],
                    },
                    "status": "question_pending",
                    "ready_to_write": False,
                },
            ) as plan_mock,
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.seed_parent_issues_with_runtime",
                side_effect=AssertionError("Incomplete PM interview should not seed Jira"),
            ) as seed_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!pm shape a rollout narrative for TP-20",
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "pm")
        self.assertIn("Examples:", command_response.message)
        self.assertTrue(command_response.data["pm_mode"])
        self.assertEqual(command_response.data["followup_context_type"], "pm_interview")
        self.assertIn("product_brief_markdown", command_response.data)
        self.assertNotIn("parent_issue_key", command_response.data)
        self.assertFalse(command_response.data["ready_to_write"])
        seed_mock.assert_not_called()
        self.assertEqual(plan_mock.call_args.kwargs["request_text"], "shape a rollout narrative for TP-20")

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            history = list((tenant.discord_config or {}).get("ask_history") or [])
            self.assertTrue(history)
            latest_entry = history[-1]
            self.assertTrue(str(latest_entry.get("question") or "").startswith("pm "))
            self.assertIn("Examples:", str(latest_entry.get("answer") or ""))

    def test_pm_command_creates_parent_issue_and_returns_parent_metadata(self) -> None:
        planning_result = SimpleNamespace(
            planning_state="planning_completed",
            required_tasks=("Implement retry telemetry", "Add fallback UX validation"),
            findings=("Telemetry coverage must be explicit.",),
            recommendations=("Keep the first cut focused on customer-visible recovery.",),
            acceptance_impacts=("Acceptance criteria must mention fallback UX.",),
            open_behavior_questions=(),
            architecture_summary=(
                "Split telemetry and UX work.",
                "One child ticket per implementation slice.",
            ),
            architecture_diagram="flowchart TD\n  Parent[Parent brief] --> Child[Engineering child]",
            stages=(
                SimpleNamespace(
                    planning_state="engineering_planning",
                    persona_id="architect",
                    to_payload=lambda: {
                        "findings": ["Split telemetry and UX work."],
                        "recommendations": ["One child ticket per implementation slice."],
                        "required_tasks": ["Implement retry telemetry"],
                        "open_behavior_questions": [],
                        "acceptance_impacts": ["Telemetry needs explicit coverage."],
                        "mermaid_diagram": "flowchart TD\n  Parent[Parent brief] --> Child[Engineering child]",
                    },
                ),
                SimpleNamespace(
                    planning_state="security_planning",
                    persona_id="security",
                    to_payload=lambda: {
                        "findings": ["Protect retry events from abuse."],
                        "recommendations": ["Add misuse checks."],
                        "required_tasks": ["Add fallback UX validation"],
                        "open_behavior_questions": [],
                        "acceptance_impacts": ["Security validation is required."],
                    },
                ),
                SimpleNamespace(
                    planning_state="test_planning",
                    persona_id="qa",
                    to_payload=lambda: {
                        "findings": ["Regression coverage is required."],
                        "recommendations": ["Automate the failure-recovery path."],
                        "required_tasks": [],
                        "open_behavior_questions": [],
                        "acceptance_impacts": ["Tests should cover visible recovery."],
                    },
                ),
            ),
        )
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_pm_interview_with_codex",
                return_value={
                    "message": "The PM brief is complete and ready for parent creation.",
                    "brief": {
                        "objective": "Ship checkout recovery",
                        "user_value": "Customers recover cleanly from checkout failures.",
                        "target_user": "Customers experiencing checkout failure",
                        "primary_journey": "Retry after a failed checkout",
                        "acceptance_criteria": [
                            "Customers can retry checkout from the failure state",
                            "Fallback UX explains what to do next",
                        ],
                        "ui_references": ["Checkout failure screen"],
                        "constraints": ["Use the existing checkout system"],
                        "success_outcomes": ["Higher recovery rate from checkout failures"],
                        "recommendation": "Focus on the customer-visible fallback first.",
                        "scope_in": ["Retry telemetry", "Fallback UX"],
                        "scope_out": ["Provider migration"],
                        "risks": ["Analytics gap"],
                        "open_questions": [],
                        "next_steps": ["Review the parent feature with product"],
                    },
                    "status": "ready_to_write",
                    "ready_to_write": True,
                },
            ) as plan_mock,
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.seed_parent_issues_with_runtime",
                return_value=(
                    "PM parent issue upsert complete. Created 1: TP-501. Updated 0: none.",
                    {
                        "created_parent_issue_keys": ["TP-501"],
                        "updated_parent_issue_keys": [],
                        "created_parent_issue_links": ["https://master-builder.atlassian.net/browse/TP-501"],
                        "updated_parent_issue_links": [],
                        "all_parent_issue_keys": ["TP-501"],
                    },
                ),
            ) as seed_mock,
            patch(
                "orchestrator.api.discord.commands.ask.run_specialist_planning_fanout",
                return_value=planning_result,
            ) as planning_mock,
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.seed_issues_with_runtime",
                return_value=(
                    "Issue upsert complete. Parent: TP-501. Created 2: TP-502, TP-503.",
                    {
                        "parent_issue_key": "TP-501",
                        "created_children": ["TP-502", "TP-503"],
                        "updated_children": [],
                        "children_sync_status": "children_current",
                    },
                ),
            ) as seed_children_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!pm final handoff for TP-20 checkout reliability",
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "pm")
        self.assertEqual(command_response.data["parent_issue_key"], "TP-501")
        self.assertEqual(command_response.data["created_parent_issue_keys"], ["TP-501"])
        self.assertEqual(command_response.data["created_children"], ["TP-502", "TP-503"])
        self.assertEqual(command_response.data["followup_context_type"], "pm_interview")
        self.assertTrue(command_response.data["ready_to_write"])
        self.assertIn("## Approved Product Brief", str(command_response.data["product_brief_markdown"]))
        self.assertIn("PM parent issue upsert complete", command_response.message)
        self.assertIn("Issue upsert complete", command_response.message)
        self.assertEqual(plan_mock.call_args.kwargs["request_text"], "final handoff for TP-20 checkout reliability")
        seed_mock.assert_called_once()
        planning_mock.assert_called_once()
        seed_children_mock.assert_called_once()

    def test_pm_ready_to_write_stage_spi_env_blocks_parent_seed(self) -> None:
        prev_spi = os.environ.get("ORCHESTRATOR_STAGE_SPI_ENABLED")
        os.environ["ORCHESTRATOR_STAGE_SPI_ENABLED"] = "true"
        get_settings.cache_clear()
        try:
            with (
                self.session_factory() as session,
                patch(
                    "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                    return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
                ),
                patch(
                    "orchestrator.api.discord.commands.ask.plan_pm_interview_with_codex",
                    return_value={
                        "message": "The PM brief is complete and ready for parent creation.",
                        "brief": {
                            "objective": "Ship checkout recovery",
                            "user_value": "Customers recover cleanly from checkout failures.",
                            "target_user": "Customers experiencing checkout failure",
                            "primary_journey": "Retry after a failed checkout",
                            "acceptance_criteria": [
                                "Customers can retry checkout from the failure state",
                                "Fallback UX explains what to do next",
                            ],
                            "ui_references": ["Checkout failure screen"],
                            "constraints": ["Use the existing checkout system"],
                            "success_outcomes": ["Higher recovery rate from checkout failures"],
                            "recommendation": "Focus on the customer-visible fallback first.",
                            "scope_in": ["Retry telemetry", "Fallback UX"],
                            "scope_out": ["Provider migration"],
                            "risks": ["Analytics gap"],
                            "open_questions": [],
                            "next_steps": ["Review the parent feature with product"],
                        },
                        "status": "ready_to_write",
                        "ready_to_write": True,
                    },
                ),
                patch("orchestrator.api.discord.ingress.seed_runtime.seed_parent_issues_with_runtime") as seed_mock,
            ):
                command_response = execute_discord_command(
                    tenant_id=self.tenant_id,
                    payload=DiscordCommandRequest(
                        user_id="u-viewer",
                        channel_id="discord-channel-1",
                        command="!pm final handoff for TP-20 checkout reliability",
                    ),
                    session=session,
                )

            self.assertTrue(command_response.ok)
            self.assertEqual(command_response.command, "pm")
            self.assertEqual(command_response.data.get("stage_plugin"), "design")
            self.assertFalse(command_response.data["stage_ready_for_implementation"])
            self.assertIn("design direction", command_response.message.lower())
            seed_mock.assert_not_called()
        finally:
            if prev_spi is None:
                os.environ.pop("ORCHESTRATOR_STAGE_SPI_ENABLED", None)
            else:
                os.environ["ORCHESTRATOR_STAGE_SPI_ENABLED"] = prev_spi
            get_settings.cache_clear()

    def test_pm_approve_is_rejected(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!pm approve rollout to beta"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !pm <product request>", response.json()["detail"])

    def test_engineer_command_returns_advisory_persona_response(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=("TP-20", "To Do", [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.personas.answer_voice_room_persona_with_codex",
                return_value={
                    "message": "Split the work by persistence, API, and validation boundaries.",
                    "brief": {"focus": "decomposition"},
                },
            ) as answer_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!engineer @TP-20 how should we split this?",
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "engineer")
        self.assertTrue(command_response.data["advisory_only"])
        self.assertEqual(command_response.data["persona_id"], "engineer")
        self.assertEqual(command_response.data["issue_key"], "TP-20")
        self.assertIn("persistence", command_response.message.lower())
        answer_mock.assert_called_once()

    def test_tester_command_maps_to_qa_persona(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.personas.answer_voice_room_persona_with_codex",
                return_value={
                    "message": "Cover the happy path and one failed validation path.",
                    "brief": {},
                },
            ) as answer_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!tester what should we verify?",
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "tester")
        self.assertEqual(command_response.data["persona_id"], "qa")
        self.assertEqual(answer_mock.call_args.kwargs["persona_id"], "qa")

    def test_pm_room_mode_routes_to_persona_room_runtime(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_pm_interview_with_codex",
                return_value={
                    "message": (
                        "What kind of PM outcome do you need here?\n"
                        "Examples: customer-facing feature brief, internal product spec, or rollout plan."
                    ),
                    "brief": {
                        "objective": "Shape the MVP for transcription and routing.",
                        "user_value": "Stakeholders can align on the first release.",
                        "recommendation": "Clarify the intended PM artifact first.",
                        "scope_in": ["Transcription", "Routing"],
                        "scope_out": ["Full architecture design"],
                        "risks": ["The product brief is still too broad."],
                        "open_questions": ["What decision should the PM help make next?"],
                        "next_steps": ["Continue the PM interview in the room thread."],
                    },
                    "status": "question_pending",
                    "ready_to_write": False,
                },
            ) as plan_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!pm how should this fit together",
                    command_params={"room_mode": "true"},
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "pm")
        self.assertTrue(command_response.data["room_mode"])
        self.assertEqual(command_response.data.get("room_source"), "text")
        self.assertEqual(command_response.data["persona_id"], "pm")
        self.assertEqual(command_response.data["persona_name"], "PM")
        self.assertEqual(command_response.data["followup_context_type"], "pm_interview")
        self.assertIn("Examples:", command_response.message)
        plan_mock.assert_called_once()

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            history = list((tenant.discord_config or {}).get("ask_history") or [])
            self.assertTrue(history)
            latest_entry = history[-1]
            self.assertTrue(str(latest_entry.get("question") or "").startswith("room "))
            self.assertTrue(str(latest_entry.get("answer") or "").startswith("pm: "))

    def test_pm_live_voice_source_routes_to_persona_runtime_with_channel_local_history(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, [{"question": "voice earlier", "answer": "security: older reply"}]),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_pm_interview_with_codex",
                return_value={
                    "message": (
                        "What product decision do you need to make about session security?\n"
                        "Examples: customer-facing requirement, scope boundary, or rollout constraint."
                    ),
                    "brief": {
                        "objective": "Clarify the product requirement for session security.",
                        "user_value": "Stakeholders understand the expected security behavior.",
                        "recommendation": "Stay product-level until the PM brief is complete.",
                        "scope_in": ["Session security requirement"],
                        "scope_out": ["Detailed security design"],
                        "risks": ["The ask mixes product and implementation concerns."],
                        "open_questions": ["Which user-visible behavior matters most?"],
                        "next_steps": ["Continue the PM interview with one focused answer."],
                    },
                    "status": "question_pending",
                    "ready_to_write": False,
                },
            ) as plan_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!pm what should we do about session security",
                    command_params={"room_source": "live_voice"},
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "pm")
        self.assertTrue(command_response.data["room_mode"])
        self.assertEqual(command_response.data.get("room_source"), "live_voice")
        self.assertEqual(command_response.data["persona_id"], "pm")
        self.assertEqual(command_response.data["persona_name"], "PM")
        self.assertEqual(command_response.data["followup_context_type"], "pm_interview")
        self.assertIn("Examples:", command_response.message)
        plan_mock.assert_called_once()
        self.assertEqual(plan_mock.call_args.kwargs["history"], [{"question": "voice earlier", "answer": "security: older reply"}])

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            history = list((tenant.discord_config or {}).get("ask_history") or [])
            self.assertTrue(history)
            latest_entry = history[-1]
            self.assertTrue(str(latest_entry.get("question") or "").startswith("voice "))
            self.assertTrue(str(latest_entry.get("answer") or "").startswith("pm: "))

    def test_ask_command_returns_board_answer(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [], {"To Do": 2}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_runtime",
                return_value="Board snapshot",
            ),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-viewer",
                    "channel_id": "discord-channel-1",
                    "command": "!ask what is on the board",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["message"], "Board snapshot")
        self.assertEqual(response.json()["data"]["status_counts"]["To Do"], 2)

    def test_ask_command_supports_issue_scope(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=("TP-101", None, [], {}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_runtime",
                return_value="Issue snapshot",
            ) as answer_mock,
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-viewer",
                    "channel_id": "discord-channel-1",
                    "command": "!ask @TP-101 summarize status",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["message"], "Issue snapshot")
        self.assertEqual(response.json()["data"]["issue_key"], "TP-101")
        self.assertEqual(answer_mock.call_args.kwargs["invocation_context"].issue_key, "TP-101")

    def test_ask_command_rejects_invalid_issue_scope_token(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!ask @bad summarize"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !ask @ISSUE-123", response.json()["detail"])

    def test_project_filter_jql_scopes_to_channel_project(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Other Project",
                "github_repository": "https://github.com/example/other",
                "jira_project_key": "OTH",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        other_project_id = create_project.json()["project_id"]

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)

            default_project = session.get(Project, f"{self.tenant_id}-default")
            self.assertIsNotNone(default_project)
            default_discord = dict(default_project.discord_config or {})
            default_discord["channel_id"] = "discord-channel-1"
            default_project.discord_config = default_discord

            other_project = session.get(Project, other_project_id)
            self.assertIsNotNone(other_project)
            other_discord = dict(other_project.discord_config or {})
            other_discord["channel_id"] = "discord-channel-2"
            other_project.discord_config = other_discord
            session.commit()

            self.assertEqual(
                project_filter_jql(session=session, tenant=tenant, channel_id="discord-channel-1"),
                'project = "TP"',
            )
            self.assertEqual(
                project_filter_jql(session=session, tenant=tenant, channel_id="discord-channel-2"),
                'project = "OTH"',
            )
            with self.assertRaises(HTTPException):
                project_filter_jql(session=session, tenant=tenant, channel_id="discord-unmapped")
            self.assertIn(
                project_filter_jql(session=session, tenant=tenant, channel_id="__dm__"),
                {'project in ("TP", "OTH")', 'project in ("OTH", "TP")'},
            )
            self.assertIn(
                project_filter_jql(session=session, tenant=tenant, channel_id="dm"),
                {'project in ("TP", "OTH")', 'project in ("OTH", "TP")'},
            )
            self.assertIn(
                project_filter_jql(session=session, tenant=tenant),
                {'project in ("TP", "OTH")', 'project in ("OTH", "TP")'},
            )

    def test_project_filter_jql_excludes_archived_projects_from_unscoped_queries(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Archived Project",
                "github_repository": "https://github.com/example/archived",
                "jira_project_key": "ARC",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        archived_project_id = create_project.json()["project_id"]

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            archived_project = session.get(Project, archived_project_id)
            self.assertIsNotNone(archived_project)
            archived_project.is_archived = True
            session.commit()

            jql = project_filter_jql(session=session, tenant=tenant, channel_id="dm")
            self.assertEqual(jql, 'project = "TP"')
            self.assertNotIn("ARC", jql)

    def test_collect_ask_context_scopes_jql_to_channel_project(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Other Project",
                "github_repository": "https://github.com/example/other",
                "jira_project_key": "OTH",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        other_project_id = create_project.json()["project_id"]

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            other_project = session.get(Project, other_project_id)
            self.assertIsNotNone(other_project)
            other_discord = dict(other_project.discord_config or {})
            other_discord["channel_id"] = "discord-channel-2"
            other_project.discord_config = other_discord
            session.commit()

            with patch("orchestrator.api.discord.ingress.ask_runtime._search_jira_issues_for_tenant", return_value=[]) as search_mock:
                collect_ask_context(
                    session=session,
                    tenant=tenant,
                    channel_id="discord-channel-2",
                    question="what changed",
                )

        called_jql = search_mock.call_args.kwargs["jql"]
        self.assertIn('project = "OTH"', called_jql)
        self.assertNotIn('project = "TP"', called_jql)

    def test_ask_board_message_passes_channel_scoped_project_key_to_codex(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Other Project",
                "github_repository": "https://github.com/example/other",
                "jira_project_key": "OTH",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        other_project_id = create_project.json()["project_id"]

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)

            default_project = session.get(Project, f"{self.tenant_id}-default")
            self.assertIsNotNone(default_project)
            default_discord = dict(default_project.discord_config or {})
            default_discord["channel_id"] = "discord-channel-1"
            default_project.discord_config = default_discord

            other_project = session.get(Project, other_project_id)
            self.assertIsNotNone(other_project)
            other_discord = dict(other_project.discord_config or {})
            other_discord["channel_id"] = "discord-channel-2"
            other_project.discord_config = other_discord
            session.commit()

            with (
                patch(
                    "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                    return_value=(
                        None,
                        None,
                        [{"key": "OTH-1", "summary": "Other item", "status": "To Do"}],
                        {"To Do": 1},
                        [],
                    ),
                ),
                patch("orchestrator.api.discord.ingress.ask_runtime.build_codex_runtime"),
                patch("orchestrator.api.discord.ingress.ask_runtime.answer_board_question_with_runtime", return_value="Board answer") as answer_mock,
            ):
                message, _ = ask_board_message(
                    session=session,
                    tenant=tenant,
                    user_id="u-viewer",
                    channel_id="discord-channel-2",
                    question="what is in progress",
                )

        self.assertEqual(message, "Board answer")
        self.assertEqual(answer_mock.call_args.kwargs["project_keys"], ["OTH"])

    def test_project_filter_jql_scopes_to_project_thread_channel(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Thread Project",
                "github_repository": "https://github.com/example/thread",
                "jira_project_key": "THR",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        project_id = create_project.json()["project_id"]

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            project = session.get(Project, project_id)
            self.assertIsNotNone(project)
            discord_config = dict(project.discord_config or {})
            discord_config["ask_thread_channel_ids"] = ["discord-project-thread-1"]
            project.discord_config = discord_config
            session.commit()

            self.assertEqual(
                project_filter_jql(session=session, tenant=tenant, channel_id="discord-project-thread-1"),
                'project = "THR"',
            )

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

    def test_ask_command_requires_confirmation_when_intent_is_action(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "command", "summary": "Queue the issue run now", "command": "!run TP-20"},
            ),
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!ask please run TP-20",
                ),
                session=session,
                require_ask_confirmation=True,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "ask")
        self.assertIsInstance(command_response.data, dict)
        self.assertTrue(command_response.data["requires_confirmation"])
        self.assertEqual(command_response.data["proposed_command"], "!run TP-20")
        self.assertTrue(command_response.data["request_id"])

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            pending = tenant.discord_config.get("pending_ask_actions", [])
            self.assertEqual(len(pending), 1)

    def test_ask_command_confirmation_uses_channel_scoped_project_keys(self) -> None:
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
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "OTH-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "Board answer"},
            ) as plan_mock,
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_runtime",
                return_value="Scoped board answer",
            ),
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-2",
                    command="!ask what is blocked?",
                ),
                session=session,
                require_ask_confirmation=True,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "ask")
        self.assertEqual(plan_mock.call_args.kwargs["project_keys"], ["OTH"])

    def test_ask_confirmation_passes_github_context_to_intent_planner(self) -> None:
        github_context = {
            "available": True,
            "repositories": [
                {
                    "repo_full_name": "example/repo",
                    "project_keys": ["TP"],
                    "open_pull_requests": [{"number": 42, "title": "Update staging flow"}],
                }
            ],
        }
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch("orchestrator.api.discord.ingress.ask_runtime.collect_github_ask_context", return_value=github_context),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "Board answer"},
            ) as plan_mock,
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_runtime",
                return_value="Board answer",
            ),
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!ask review staging against current tickets",
                ),
                session=session,
                require_ask_confirmation=True,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(plan_mock.call_args.kwargs["github_context"], github_context)

    def test_ask_follow_up_reuses_recent_scoped_issue_key(self) -> None:
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
            patch("orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context", side_effect=_collect_stub),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch("orchestrator.api.discord.commands.ask.answer_board_question_with_runtime", return_value="Board answer"),
        ):
            first = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!ask @TP-77 summarize status",
                ),
                session=session,
            )
            second = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!ask what changed since last update?",
                ),
                session=session,
            )

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(collect_calls[0], "TP-77")
        self.assertEqual(collect_calls[1], "TP-77")

    def test_ask_board_message_passes_github_context_to_codex(self) -> None:
        github_context = {
            "available": True,
            "repositories": [
                {
                    "repo_full_name": "example/repo",
                    "project_keys": ["TP"],
                    "open_pull_requests": [{"number": 7, "title": "Refactor worker"}],
                }
            ],
        }
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [], {}, []),
            ),
            patch("orchestrator.api.discord.ingress.ask_runtime.collect_github_ask_context", return_value=github_context),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch("orchestrator.api.discord.commands.ask.answer_board_question_with_runtime", return_value="Board answer") as answer_mock,
        ):
            response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!ask compare staging to in-progress tickets",
                ),
                session=session,
            )

        self.assertTrue(response.ok)
        self.assertEqual(answer_mock.call_args.kwargs["github_context"], github_context)

    def test_collect_github_ask_context_partitions_staging_prs(self) -> None:
        fake_prs = [
            SimpleNamespace(
                number=1,
                title="Feature to staging",
                state="open",
                head_ref="jira/feature-1",
                base_ref="staging",
                html_url="https://github.com/example/repo/pull/1",
                updated_at="2026-02-12T17:00:00Z",
            ),
            SimpleNamespace(
                number=2,
                title="Feature to main",
                state="open",
                head_ref="jira/feature-2",
                base_ref="main",
                html_url="https://github.com/example/repo/pull/2",
                updated_at="2026-02-12T17:05:00Z",
            ),
        ]
        fake_client = SimpleNamespace(list_open_pull_requests=lambda **_kwargs: fake_prs)
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            with (
                patch("orchestrator.api.discord.ingress.ask_runtime.github_client_from_tenant_config", return_value=fake_client),
                patch(
                    "orchestrator.api.discord.ingress.ask_runtime.collect_local_repo_context",
                    return_value=SimpleNamespace(
                        available=True,
                        reason=None,
                        repo_dir="/tmp/repo",
                        current_branch="staging",
                        head_sha="abc123",
                        branches=["staging", "jira/TP-1"],
                        recent_commits=["abc123 TP-1: update"],
                    ),
                ),
            ):
                context = collect_github_ask_context(
                    session=session,
                    tenant=tenant,
                    project_keys=["TP"],
                )

        self.assertTrue(context["available"])
        repositories = context["repositories"]
        self.assertEqual(len(repositories), 1)
        self.assertEqual(len(repositories[0]["open_pull_requests"]), 2)
        self.assertEqual(len(repositories[0]["staging_pull_requests"]), 1)
        self.assertEqual(repositories[0]["staging_pull_requests"][0]["number"], 1)
        self.assertTrue(repositories[0]["local_repo"]["available"])
        self.assertEqual(repositories[0]["local_repo"]["current_branch"], "staging")

    def test_collect_github_ask_context_marks_degraded_when_pr_fetch_fails(self) -> None:
        fake_client = SimpleNamespace(
            list_open_pull_requests=lambda **_kwargs: (_ for _ in ()).throw(GitHubApiError("rate limited"))
        )
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            with (
                patch("orchestrator.api.discord.ingress.ask_runtime.github_client_from_tenant_config", return_value=fake_client),
                patch(
                    "orchestrator.api.discord.ingress.ask_runtime.collect_local_repo_context",
                    return_value=SimpleNamespace(
                        available=False,
                        reason="repository_not_cloned",
                        repo_dir="/tmp/repo",
                        current_branch=None,
                        head_sha=None,
                        branches=[],
                        recent_commits=[],
                    ),
                ),
            ):
                context = collect_github_ask_context(
                    session=session,
                    tenant=tenant,
                    project_keys=["TP"],
                )

        self.assertFalse(context["available"])
        self.assertEqual(context["reason"], "github_pull_requests_unavailable")
        self.assertIn("example/repo", context["degraded_repositories"])

    def test_collect_github_ask_context_uses_platform_for_unscoped_refs(self) -> None:
        fake_client = SimpleNamespace(
            list_open_pull_requests=lambda **_kwargs: []
        )

        def _scoped_secret_lookup(
            session,
            secret_ref: str,
            encryption_key: str,
            tenant_id: str,
            project_id: str | None = None,
        ) -> str | None:
            self.assertEqual(encryption_key, get_settings().secrets_encryption_key)
            self.assertEqual(tenant_id, self.tenant_id)
            if secret_ref == f"tenant/{self.tenant_id}/GITHUB_APP_ID":
                return "tenant-app-id"
            if secret_ref == f"tenant/{self.tenant_id}/GITHUB_APP_PRIVATE_KEY":
                return "tenant-private-key"
            return None

        def _platform_secret_lookup(session, *, secret_ref: str, encryption_key: str, allow_environment_fallback: bool = True) -> str | None:
            self.assertEqual(encryption_key, get_settings().secrets_encryption_key)
            if secret_ref == "GITHUB_APP_ID":
                return "platform-app-id"
            if secret_ref == "GITHUB_APP_PRIVATE_KEY":
                return "platform-private-key"
            return None

        def _github_client_factory(
            config: dict,
            *,
            tenant_secret_lookup=None,
            platform_secret_lookup=None,
            **_: object,
        ) -> SimpleNamespace:
            self.assertIsNotNone(tenant_secret_lookup)
            self.assertIsNotNone(platform_secret_lookup)
            self.assertEqual(
                tenant_secret_lookup(f"tenant/{self.tenant_id}/GITHUB_APP_ID"),
                "tenant-app-id",
            )
            self.assertEqual(
                tenant_secret_lookup(f"tenant/{self.tenant_id}/GITHUB_APP_PRIVATE_KEY"),
                "tenant-private-key",
            )
            self.assertEqual(platform_secret_lookup("GITHUB_APP_ID"), "platform-app-id")
            self.assertEqual(platform_secret_lookup("GITHUB_APP_PRIVATE_KEY"), "platform-private-key")
            return fake_client

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            with (
                patch("orchestrator.api.discord.ingress.ask_runtime.resolve_scoped_secret_ref", side_effect=_scoped_secret_lookup),
                patch(
                    "orchestrator.api.discord.ingress.ask_runtime.resolve_platform_secret_ref",
                    side_effect=_platform_secret_lookup,
                ),
                patch("orchestrator.api.discord.ingress.ask_runtime.github_client_from_tenant_config", side_effect=_github_client_factory),
                patch(
                    "orchestrator.api.discord.ingress.ask_runtime.collect_local_repo_context",
                    return_value=SimpleNamespace(
                        available=True,
                        reason=None,
                        repo_dir="/tmp/repo",
                        current_branch="staging",
                        head_sha="abc123",
                        branches=["staging", "jira/TP-1"],
                        recent_commits=[],
                    ),
                ),
            ):
                context = collect_github_ask_context(
                    session=session,
                    tenant=tenant,
                    project_keys=["TP"],
                )

        self.assertTrue(context["available"])
        self.assertEqual(len(context["repositories"]), 1)
        self.assertEqual(context["repositories"][0]["repo_full_name"], "example/repo")

    def test_ask_history_scope_isolated_by_channel(self) -> None:
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

        collect_calls: list[tuple[str | None, str | None]] = []

        def _collect_stub(*, channel_id, scoped_issue_key, **_kwargs):  # type: ignore[no-untyped-def]
            collect_calls.append((channel_id, scoped_issue_key))
            return (
                scoped_issue_key.strip().upper() if isinstance(scoped_issue_key, str) and scoped_issue_key.strip() else None,
                None,
                [{"key": "TP-77", "summary": "Investigate", "status": "To Do"}],
                {"To Do": 1},
                [],
            )

        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context", side_effect=_collect_stub),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch("orchestrator.api.discord.commands.ask.answer_board_question_with_runtime", return_value="Board answer"),
        ):
            first = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!ask @TP-77 summarize status",
                ),
                session=session,
            )
            second = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-2",
                    command="!ask what changed since last update?",
                ),
                session=session,
            )

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(collect_calls[0], ("discord-channel-1", "TP-77"))
        self.assertEqual(collect_calls[1], ("discord-channel-2", None))

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

    def test_ask_command_surfaces_jira_provider_outage(self) -> None:
        with patch(
            "orchestrator.api.discord.ingress.ask_runtime._search_jira_issues_for_tenant",
            side_effect=HTTPException(status_code=502, detail="Failed to query Jira board: Bad Gateway"),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!ask what is blocked"},
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("Failed to query Jira board", response.json()["detail"])

    def test_ask_command_with_dm_channel_uses_tenant_scope(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.ingress.ask_runtime._search_jira_issues_for_tenant",
                return_value=[JiraIssuePreview(key="TP-50", summary="DM issue", status="To Do")],
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch("orchestrator.api.discord.commands.ask.answer_board_question_with_runtime", return_value="DM scoped answer"),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-viewer", "channel_id": None, "command": "!ask what is on the board"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "DM scoped answer")

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
                "Gap analysis for [TP-77](https://master-builder.atlassian.net/browse/TP-77)",
                {
                    "issue_key": "TP-77",
                    "jira_url": "https://master-builder.atlassian.net/browse/TP-77",
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

    def test_bug_command_requires_summary(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!bug"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !bug", response.json()["detail"])

    def test_bug_command_creates_jira_bug_from_params_and_attachments(self) -> None:
        with patch(
            "orchestrator.api.discord.ingress.bug_runtime.create_discord_bug_issue",
            return_value=(
                "Bug logged: [TP-501](https://master-builder.atlassian.net/browse/TP-501)",
                {"created_issue_keys": ["TP-501"]},
            ),
        ) as create_bug_mock:
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-viewer",
                    "channel_id": "discord-channel-1",
                    "command": "!bug",
                    "command_params": {
                        "summary": "Login fails on mobile",
                        "details": "Tap login, spinner loops forever.",
                        "issue_key": "TP-77",
                    },
                    "attachments": [
                        {
                            "id": "a1",
                            "filename": "screenshot.png",
                            "url": "https://cdn.discordapp.com/attachments/1.png",
                            "content_type": "image/png",
                        }
                    ],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["command"], "bug")
        self.assertIn("TP-501", response.json()["message"])
        create_bug_mock.assert_called_once()
        kwargs = create_bug_mock.call_args.kwargs
        self.assertEqual(kwargs["summary"], "Login fails on mobile")
        self.assertEqual(kwargs["related_issue_key"], "TP-77")
        self.assertEqual(kwargs["attachments"][0]["filename"], "screenshot.png")

    def test_bug_creation_uploads_discord_attachments_to_jira_issue(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            jira_config = dict(tenant.jira_config)
            jira_config["connection_id"] = "conn-1"
            tenant.jira_config = jira_config
            session.add(
                JiraOAuthConnection(
                    connection_id="conn-1",
                    account_id="acct-1",
                    account_email="dev@example.com",
                    cloud_id="cloud-1",
                    site_url="https://master-builder.atlassian.net",
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
                self.upload_calls: list[dict] = []
                self.created_issues: list[object] = []

            def create_issues_bulk(self, **kwargs: object) -> JiraIssueBulkCreateResult:  # noqa: ANN003
                self.created_issues = list(kwargs.get("issues") or [])
                return JiraIssueBulkCreateResult(
                    created=[JiraIssueCreateResult(key="TP-901", issue_id="901")],
                    errors=[],
                )

            def upload_issue_attachment(self, **kwargs: object) -> list[dict]:  # noqa: ANN003
                self.upload_calls.append(dict(kwargs))
                return [{"id": "att-1"}]

        fake_client = _FakeClient()
        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.jira_runtime.refresh_jira_connection_tokens", return_value="token"),
            patch("orchestrator.api.discord.ingress.jira_runtime.jira_oauth_client", return_value=fake_client),
            patch("orchestrator.api.discord.ingress.bug_runtime.resolve_discord_channel_name", return_value="triage-bugs"),
            patch(
                "orchestrator.api.discord.ingress.bug_runtime.download_discord_attachment",
                return_value=(b"image-bytes", "image/png"),
            ),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            message, data = create_discord_bug_issue(
                session=session,
                tenant=tenant,
                summary="Login fails",
                details="See screenshot",
                reporter_user_id="u-viewer",
                channel_id="discord-channel-1",
                related_issue_key=None,
                attachments=[{"filename": "screen.png", "url": "https://cdn.discordapp.com/x.png"}],
                selected_project_key="TP",
            )

        self.assertIn("Attached 1/1 file(s)", message)
        self.assertEqual(len(fake_client.upload_calls), 1)
        self.assertEqual(fake_client.upload_calls[0]["issue_id_or_key"], "TP-901")
        self.assertEqual(len(fake_client.created_issues), 1)
        created_description = str(fake_client.created_issues[0].description)
        self.assertIn("Channel: triage-bugs (discord-channel-1)", created_description)
        self.assertIn("screen.png", created_description)
        self.assertNotIn("https://cdn.discordapp.com/x.png", created_description)

    def test_bug_creation_fails_hard_on_attachment_failure(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            jira_config = dict(tenant.jira_config)
            jira_config["connection_id"] = "conn-attach-fail"
            tenant.jira_config = jira_config
            session.add(
                JiraOAuthConnection(
                    connection_id="conn-attach-fail",
                    account_id="acct-1",
                    account_email="dev@example.com",
                    cloud_id="cloud-1",
                    site_url="https://master-builder.atlassian.net",
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
                self.created_issues: list[object] = []

            def create_issues_bulk(self, **kwargs: object) -> JiraIssueBulkCreateResult:  # noqa: ANN003
                self.created_issues = list(kwargs.get("issues") or [])
                return JiraIssueBulkCreateResult(
                    created=[JiraIssueCreateResult(key="TP-903", issue_id="903")],
                    errors=[],
                )

            def upload_issue_attachment(self, **kwargs: object) -> list[dict]:  # noqa: ANN003
                del kwargs
                raise JiraOAuthError("Jira attachment upload failed (403): permission denied")

        fake_client = _FakeClient()
        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.jira_runtime.refresh_jira_connection_tokens", return_value="token"),
            patch("orchestrator.api.discord.ingress.jira_runtime.jira_oauth_client", return_value=fake_client),
            patch("orchestrator.api.discord.ingress.bug_runtime.resolve_discord_channel_name", return_value="triage-bugs"),
            patch(
                "orchestrator.api.discord.ingress.bug_runtime.download_discord_attachment",
                return_value=(b"image-bytes", "image/png"),
            ),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            with self.assertRaises(HTTPException) as exc:
                create_discord_bug_issue(
                    session=session,
                    tenant=tenant,
                    summary="Login fails",
                    details="See screenshot",
                    reporter_user_id="u-viewer",
                    channel_id="discord-channel-1",
                    related_issue_key=None,
                    attachments=[{"filename": "screen.png", "url": "https://cdn.discordapp.com/x.png"}],
                    selected_project_key="TP",
                )
        self.assertEqual(exc.exception.status_code, 502)
        self.assertIn("Bug created as TP-903", str(exc.exception.detail))
        self.assertIn("permission denied", str(exc.exception.detail))
        self.assertIn("Jira upload", str(exc.exception.detail))

    def test_bug_creation_falls_back_to_channel_id_when_name_lookup_unavailable(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            jira_config = dict(tenant.jira_config)
            jira_config["connection_id"] = "conn-2"
            tenant.jira_config = jira_config
            session.add(
                JiraOAuthConnection(
                    connection_id="conn-2",
                    account_id="acct-1",
                    account_email="dev@example.com",
                    cloud_id="cloud-1",
                    site_url="https://master-builder.atlassian.net",
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
                self.created_issues: list[object] = []

            def create_issues_bulk(self, **kwargs: object) -> JiraIssueBulkCreateResult:  # noqa: ANN003
                self.created_issues = list(kwargs.get("issues") or [])
                return JiraIssueBulkCreateResult(
                    created=[JiraIssueCreateResult(key="TP-902", issue_id="902")],
                    errors=[],
                )

            def upload_issue_attachment(self, **kwargs: object) -> list[dict]:  # noqa: ANN003
                return [{"id": "att-1"}]

        fake_client = _FakeClient()
        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.jira_runtime.refresh_jira_connection_tokens", return_value="token"),
            patch("orchestrator.api.discord.ingress.jira_runtime.jira_oauth_client", return_value=fake_client),
            patch("orchestrator.api.discord.ingress.bug_runtime.resolve_discord_channel_name", return_value=None),
            patch(
                "orchestrator.api.discord.ingress.bug_runtime.download_discord_attachment",
                return_value=(b"image-bytes", "image/png"),
            ),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            create_discord_bug_issue(
                session=session,
                tenant=tenant,
                summary="Login fails",
                details="See screenshot",
                reporter_user_id="u-viewer",
                channel_id="discord-channel-1",
                related_issue_key=None,
                attachments=[{"filename": "screen.png", "url": "https://cdn.discordapp.com/x.png"}],
                selected_project_key="TP",
            )
        description = str(fake_client.created_issues[0].description)
        self.assertIn("Channel: discord-channel-1", description)

    def test_bug_creation_uses_selected_scoped_project_key(self) -> None:
        now = datetime.now(timezone.utc)
        self._create_project(project_id=f"{self.tenant_id}-other", jira_project_key="OTH", channel_id="discord-other-1")
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            jira_config = dict(tenant.jira_config)
            jira_config["connection_id"] = "conn-3"
            tenant.jira_config = jira_config
            session.add(
                JiraOAuthConnection(
                    connection_id="conn-3",
                    account_id="acct-1",
                    account_email="dev@example.com",
                    cloud_id="cloud-1",
                    site_url="https://master-builder.atlassian.net",
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
                self.project_key: str | None = None

            def create_issues_bulk(self, **kwargs: object) -> JiraIssueBulkCreateResult:  # noqa: ANN003
                self.project_key = str(kwargs.get("project_key"))
                return JiraIssueBulkCreateResult(
                    created=[JiraIssueCreateResult(key="OTH-902", issue_id="902")],
                    errors=[],
                )

            def upload_issue_attachment(self, **kwargs: object) -> list[dict]:  # noqa: ANN003
                return []

        fake_client = _FakeClient()
        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.jira_runtime.refresh_jira_connection_tokens", return_value="token"),
            patch("orchestrator.api.discord.ingress.jira_runtime.jira_oauth_client", return_value=fake_client),
            patch("orchestrator.api.discord.ingress.bug_runtime.resolve_discord_channel_name", return_value="other"),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            create_discord_bug_issue(
                session=session,
                tenant=tenant,
                summary="Scoped bug",
                details="Details",
                reporter_user_id="u-viewer",
                channel_id="discord-other-1",
                related_issue_key=None,
                attachments=[],
                selected_project_key="OTH",
            )

        self.assertEqual(fake_client.project_key, "OTH")

    def test_bug_creation_requires_scoped_project_key(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            with self.assertRaises(HTTPException) as exc:
                create_discord_bug_issue(
                    session=session,
                    tenant=tenant,
                    summary="Missing scope",
                    details="Details",
                    reporter_user_id="u-viewer",
                    channel_id="discord-channel-1",
                    related_issue_key=None,
                    attachments=[],
                    selected_project_key=None,
                )
        self.assertEqual(exc.exception.status_code, 409)
        self.assertIn("project-scoped", str(exc.exception.detail))

    def test_build_discord_bug_description_lists_attachments_without_hyperlinks(self) -> None:
        description = build_discord_bug_description(
            summary="Login fails",
            details="Details",
            reporter_user_id="u-viewer",
            channel_id="triage-bugs (discord-channel-1)",
            related_issue_key="TP-77",
            attachments=[{"filename": "screen.png", "url": "https://cdn.discordapp.com/x.png"}],
        )
        self.assertIn("screen.png", description)
        self.assertNotIn("https://cdn.discordapp.com/x.png", description)
        self.assertIn("Channel: triage-bugs (discord-channel-1)", description)
        self.assertIn("Reported via Discord", description)
        self.assertIn("Summary", description)

    def test_request_creates_pending_request(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={
                "user_id": "u-viewer",
                "channel_id": "discord-channel-1",
                "command": "!request run_controls Need run controls",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIn("Allowlist request", response.json()["message"])

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            project = session.get(Project, f"{self.tenant_id}-default")
            self.assertIsNotNone(project)
            requests = (project.discord_config or {}).get("allowlist_requests", [])
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["user_id"], "u-viewer")
            self.assertEqual(requests[0]["permissions"], ["run_controls"])
            self.assertEqual(requests[0]["reason"], "Need run controls")

    def test_request_for_allowlisted_user_returns_already_allowlisted(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={
                "user_id": "u-admin",
                "channel_id": "discord-channel-1",
                "command": "!request run_controls Please add me",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIn("already allowlisted", response.json()["message"])
