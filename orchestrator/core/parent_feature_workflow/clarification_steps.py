from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.clarification.questions import ClarificationQuestion
from orchestrator.core.workflow.step_runner import (
    complete_workflow_step_attempt,
    fail_workflow_step_attempt,
    start_workflow_step_attempt,
    wait_workflow_step_attempt,
)
from orchestrator.core.workflow.execution_projection import classify_external_workflow_failure
from orchestrator.core.parent_feature_workflow.operations import (
    PARENT_OP_DISCORD_FOLLOWUP_PROJECTION,
    PARENT_OP_JIRA_COMMENT_PROJECTION,
    PARENT_OP_JIRA_PARENT_UPDATE,
)


@dataclass(frozen=True)
class ParentPlanningClarificationStepRunner:
    clarification_service: object
    issue_gateway: object

    def update_sync_label(self, *, lifecycle, parent_detail, target_label: str) -> None:  # noqa: ANN001
        step = start_workflow_step_attempt(lifecycle=lifecycle, operation_type=PARENT_OP_JIRA_PARENT_UPDATE)
        try:
            self.issue_gateway.update_issue_sync_label(
                issue_detail=parent_detail,
                target_label=target_label,
            )
        except Exception as exc:  # noqa: BLE001
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=step,
                category=classify_external_workflow_failure(error=exc),
                message=str(exc),
            )
            raise
        complete_workflow_step_attempt(
            lifecycle=lifecycle,
            step=step,
            summary=f"Updated parent issue sync label to {target_label}.",
        )

    def mark_issues_sync_blocked(self, *, lifecycle, issue_keys: list[str]) -> None:  # noqa: ANN001
        step = start_workflow_step_attempt(lifecycle=lifecycle, operation_type=PARENT_OP_JIRA_PARENT_UPDATE)
        try:
            self.issue_gateway.mark_issues_sync_blocked(issue_keys=issue_keys)
        except Exception as exc:  # noqa: BLE001
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=step,
                category=classify_external_workflow_failure(error=exc),
                message=str(exc),
            )
            raise
        complete_workflow_step_attempt(
            lifecycle=lifecycle,
            step=step,
            summary="Marked parent/child Jira issues sync-blocked.",
        )

    def publish_then_wait(
        self,
        *,
        lifecycle,
        blocking_step,
        parent_detail,
        questions: tuple[ClarificationQuestion, ...],
        context: str,
    ) -> None:  # noqa: ANN001
        required_questions = self.clarification_service.require_questions_for_waiting_state(
            questions=questions,
            context=context,
        )
        try:
            self.update_sync_label(
                lifecycle=lifecycle,
                parent_detail=parent_detail,
                target_label="sync-blocked",
            )
            publication = self.ensure_projection_attempts(
                lifecycle=lifecycle,
                issue_key=parent_detail.key,
                questions=required_questions,
            )
        except Exception as exc:  # noqa: BLE001
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=blocking_step,
                category=classify_external_workflow_failure(error=exc),
                message=str(exc),
            )
            raise
        if not publication.jira_comment_id:
            message = f"Jira clarification projection for {parent_detail.key} did not persist a comment id"
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=blocking_step,
                category="contract_violation",
                message=message,
            )
            raise RuntimeError(message)
        wait_workflow_step_attempt(
            lifecycle=lifecycle,
            step=blocking_step,
            summary=self.clarification_service.build_missing_input_message(
                issue_key=parent_detail.key,
                questions=required_questions,
            ),
        )

    def ensure_projection_attempts(
        self,
        *,
        lifecycle,
        issue_key: str,
        questions: tuple[ClarificationQuestion, ...],
    ):  # noqa: ANN201, ANN001
        active_effects = self.issue_gateway.active_clarification_effects(issue_key=issue_key, questions=questions)
        if active_effects is not None:
            if not active_effects.jira_comment_id:
                raise RuntimeError(f"Active clarification for {issue_key} has no persisted Jira comment id")
            return self.clarification_service.ensure_active_clarification(
                issue_key=issue_key,
                questions=questions,
                publisher=self.issue_gateway,
            )

        discord_step = start_workflow_step_attempt(
            lifecycle=lifecycle,
            operation_type=PARENT_OP_DISCORD_FOLLOWUP_PROJECTION,
        )
        jira_step = start_workflow_step_attempt(
            lifecycle=lifecycle,
            operation_type=PARENT_OP_JIRA_COMMENT_PROJECTION,
        )
        try:
            publication = self.clarification_service.ensure_active_clarification(
                issue_key=issue_key,
                questions=questions,
                publisher=self.issue_gateway,
            )
        except Exception as exc:  # noqa: BLE001
            category = classify_external_workflow_failure(error=exc)
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=discord_step,
                category=category,
                message=str(exc),
            )
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=jira_step,
                category=category,
                message=str(exc),
            )
            raise

        if publication.discord_followup_created:
            complete_workflow_step_attempt(
                lifecycle=lifecycle,
                step=discord_step,
                summary="Posted PM clarification follow-up to Discord.",
            )
        else:
            complete_workflow_step_attempt(
                lifecycle=lifecycle,
                step=discord_step,
                summary=f"Optional Discord clarification follow-up was not created for {issue_key}.",
            )
        if publication.jira_comment_id:
            complete_workflow_step_attempt(
                lifecycle=lifecycle,
                step=jira_step,
                summary=f"Posted PM clarification questions to Jira comment {publication.jira_comment_id}.",
            )
            return publication

        fail_workflow_step_attempt(
            lifecycle=lifecycle,
            step=jira_step,
            category="contract_violation",
            message=f"Jira clarification projection did not run for {issue_key}",
        )
        raise RuntimeError(f"Clarification projection did not run for {issue_key}")
