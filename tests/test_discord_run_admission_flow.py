from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestrator.core.pm.interview_service import (
    PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT,
    PM_INTERVIEW_STATUS_PM_COMPLETED,
)
from orchestrator.core.runs.service import EnqueueRunResult
from orchestrator.core.runs.enqueue_types import EnqueueFailureReason
from orchestrator.storage.models import (
    PMInterviewCase,
    WorkflowExecution,
    WorkflowOperation,
    WorkflowOperationAttempt,
    WorkflowOperationWorkUnit,
)
from orchestrator.tools.atlassian_oauth import JiraIssueDetail, JiraIssuePreview
from tests.test_support.discord_command_reply_harness import DiscordCommandReplyHarness


pytestmark = pytest.mark.contract


class DiscordRunAdmissionFlowTests(DiscordCommandReplyHarness):
    def _seed_self_executable_parent_planning(self, *, issue_key: str) -> None:
        now = datetime.now(timezone.utc)
        workflow_id = f"parent_planning:{issue_key}"
        with self.session_factory() as session:
            session.add(
                WorkflowExecution(
                    workflow_id=workflow_id,
                    workflow_type_key="parent_planning",
                    tenant_id=self.tenant_id,
                    project_id=self.default_project_id,
                    source_system="jira",
                    source_ref=issue_key,
                    display_name="Signed audit downloads",
                    source_description="Parent brief",
                    repo_url="https://github.com/example/repo",
                    branch=None,
                    pr_url=None,
                    orchestration_backend="legacy",
                    dedupe_scope="parent_planning",
                    status="completed",
                    last_error=None,
                    active_run_id=None,
                    latest_checkpoint_id=None,
                    source_workflow_id=None,
                    source_run_id=None,
                    created_at=now,
                    started_at=now,
                    finished_at=now,
                    updated_at=now,
                )
            )
            session.add(
                PMInterviewCase(
                    case_id=f"pm-snapshot-{issue_key}",
                    tenant_id=self.tenant_id,
                    project_id=self.default_project_id,
                    request_id=f"pm-snapshot-{issue_key}",
                    parent_issue_key=issue_key,
                    source_kind=PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT,
                    status=PM_INTERVIEW_STATUS_PM_COMPLETED,
                    channel_id="",
                    thread_channel_id=None,
                    root_message_id=None,
                    owner_user_id=None,
                    source_text="",
                    brief_json={
                        "objective": "Generate signed downloads for queued audit export requests.",
                        "user_value": "Administrators can securely retrieve completed exports.",
                        "acceptance_criteria": ["Signed download URLs are generated for completed export jobs."],
                        "scope_in": ["Signed URL generation"],
                        "scope_out": ["Changing export generation"],
                        "constraints": ["Links must expire."],
                        "risks": ["Leaked URLs expose exports."],
                        "success_outcomes": ["Audit exports can be downloaded securely."],
                        "recommendation": "Implement signed download creation directly on the source task.",
                    },
                    evidence_json=[],
                    question_history_json=[],
                    current_question_json={},
                    next_question_json={},
                    missing_slots_json=[],
                    notes_json={},
                    created_at=now,
                    updated_at=now,
                    closed_at=now,
                )
            )
            operation = WorkflowOperation(
                operation_id=f"operation-backlog-{issue_key}",
                workflow_id=workflow_id,
                run_id=None,
                operation_type="backlog_planning",
                idempotency_key="workflow-definition:backlog_planning",
                status="completed",
                target_system="jira",
                target_ref=issue_key,
                summary="Backlog planning completed.",
                created_at=now,
                started_at=now,
                finished_at=now,
                updated_at=now,
            )
            attempt = WorkflowOperationAttempt(
                attempt_id=f"attempt-backlog-{issue_key}",
                operation_id=operation.operation_id,
                attempt_number=1,
                status="completed",
                retryable=False,
                next_retry_at=None,
                created_at=now,
                started_at=now,
                finished_at=now,
            )
            work_unit = WorkflowOperationWorkUnit(
                work_unit_id=f"work-unit-planning-package-{issue_key}",
                operation_id=operation.operation_id,
                parent_attempt_id=attempt.attempt_id,
                unit_key="backlog_planning.package_assembly",
                unit_kind="assembly",
                idempotency_key=f"{issue_key}-planning-package",
                input_fingerprint=f"fingerprint-{issue_key}",
                status="completed",
                output_json={
                    "planning_package": {
                        "planning_state": "planning_completed",
                        "specialist_outputs": {},
                        "child_issues": [],
                        "technical_decisions": [],
                        "pm_decision_requests": [],
                    }
                },
                created_at=now,
                updated_at=now,
                completed_at=now,
            )
            session.add_all([operation, attempt, work_unit])
            session.commit()

    def test_run_completed_self_executable_parent_bypasses_decision_gate_and_queues_derived_contract(self) -> None:
        issue_key = "TP-255"
        self._seed_self_executable_parent_planning(issue_key=issue_key)
        captured: dict[str, object] = {}

        def _enqueue(*_args, **kwargs):  # noqa: ANN001
            captured.update(kwargs)
            return EnqueueRunResult(
                enqueued=True,
                reason=None,
                run=SimpleNamespace(run_id="run-self-executable", status="queued"),
            )

        with (
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
                return_value=JiraIssuePreview(key=issue_key, summary="Signed audit downloads", status="To Do"),
            ),
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_detail",
                return_value=JiraIssueDetail(
                    key=issue_key,
                    summary="Signed audit downloads",
                    status="To Do",
                    description="PM parent brief still lives on Jira.",
                    issue_type="Task",
                    labels=["pm-parent", "sync-current"],
                ),
            ),
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.evaluate_issue_clarification_state",
                side_effect=AssertionError("self-executable parent should not enter Decision Gate evaluation"),
            ),
            patch("orchestrator.api.discord.commands.run_controls.enqueue_issue_run_with_precheck", side_effect=_enqueue),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": f"!run {issue_key}"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["issue_key"], issue_key)
        self.assertIn("Owned by Engineering", str(captured["issue_description"]))
        self.assertNotEqual(captured["issue_description"], "PM parent brief still lives on Jira.")

    def test_run_rejects_when_ready_label_is_missing(self) -> None:
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
                    labels=[],
                ),
            ),
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.evaluate_issue_clarification_state",
                return_value=self._missing_ready_decision_result(),
            ),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!run TP-20"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("missing the configured ready label", response.json()["detail"])
        self.assertIn("agent:ready", response.json()["detail"])

    def test_run_surfaces_typed_enqueue_conflict_for_active_run(self) -> None:
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
                    labels=["agent:ready"],
                ),
            ),
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.evaluate_issue_clarification_state",
                return_value=self._ready_decision_result(),
            ),
            patch(
                "orchestrator.api.discord.commands.run_controls.enqueue_issue_run_with_precheck",
                return_value=EnqueueRunResult(
                    enqueued=False,
                    reason=EnqueueFailureReason.RUN_ALREADY_ACTIVE,
                    run=SimpleNamespace(run_id="run-1", status="queued"),
                ),
            ),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!run TP-20"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("run_already_active", response.json()["detail"])
        self.assertIn("run-1", response.json()["detail"])
