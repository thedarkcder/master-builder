from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from orchestrator.core.clarification_questions import ClarificationQuestion, ClarificationQuestionSet
from orchestrator.core.parent_planning_clarification_service import (
    ParentPlanningClarificationService,
)
from orchestrator.core.parent_planning_fanout_service import (
    ParentPlanningFanoutResult,
    ParentPlanningFanoutSeedError,
    ParentPlanningFanoutService,
)
from orchestrator.core.parent_feature_workflow.operations import (
    PARENT_OP_BACKLOG_PLANNING,
    PARENT_OP_BRIEF_NORMALIZATION,
    PARENT_OP_DISCORD_FOLLOWUP_PROJECTION,
    PARENT_OP_JIRA_CHILD_FANOUT,
    PARENT_OP_JIRA_CHILD_PROMOTION,
    PARENT_OP_JIRA_COMMENT_PROJECTION,
    PARENT_OP_JIRA_PARENT_UPDATE,
    PARENT_OP_NOTIFICATION_EMIT,
)
from orchestrator.core.specialist_planning import PLANNING_STATE_COMPLETED
from orchestrator.core.workflow_advance import WorkflowAdvanceLifecycle, WorkflowAdvanceOutcome
from orchestrator.core.workflow_definition import WorkflowStepKind, workflow_step
from orchestrator.core.workflow_execution_projection import classify_external_workflow_failure
from orchestrator.core.workflow_step_runner import (
    complete_workflow_step_attempt,
    fail_workflow_step_attempt,
    start_workflow_step_attempt,
    wait_workflow_step_attempt,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParentFeaturePlanningWorkflowDeps:
    issue_gateway: Any
    brief_planner: Any
    child_sync_gateway: Any
    clarification_service: ParentPlanningClarificationService
    fanout_service: ParentPlanningFanoutService
    workflow_type: Any
    project_key_for_issue_fn: Callable[[str], str]
    extract_changed_fields_fn: Callable[..., list[str]]
    extract_status_transition_fn: Callable[..., tuple[str | None, str | None]]
    material_parent_changed_fields_fn: Callable[..., list[str]]
    parent_board_entry_target_status_fn: Callable[..., str | None]


class ParentFeaturePlanningWorkflow:
    def __init__(self, *, deps: ParentFeaturePlanningWorkflowDeps) -> None:
        self._deps = deps

    def handle(
        self,
        *,
        context,
        session: Session,
        settings,  # noqa: ANN001
        lifecycle: WorkflowAdvanceLifecycle,
    ) -> WorkflowAdvanceOutcome:
        if bool(context.payload.get("_mb_pm_interview_followup")):
            return self._handle_pm_interview_followup(
                context=context,
                session=session,
                settings=settings,
                lifecycle=lifecycle,
            )
        normalized_labels = {str(label).strip().casefold() for label in context.issue_labels or []}
        if context.webhook_event not in {"issue_created", "issue_updated"} or "pm-parent" not in normalized_labels:
            return self._outcome(handled=False)
        routed_from_backlog = bool(context.payload.get("_mb_pm_parent_routed_from_backlog"))
        if context.webhook_event == "issue_created" or routed_from_backlog:
            return self._handle_issue_created(context=context, session=session, settings=settings, lifecycle=lifecycle)
        return self._handle_issue_updated(context=context, session=session, settings=settings, lifecycle=lifecycle)

    def _outcome(
        self,
        *,
        handled: bool,
        reason: str | None = None,
        extra: dict[str, object] | None = None,
        requires_persisted_execution: bool | None = None,
        failed: bool = False,
    ) -> WorkflowAdvanceOutcome:
        return WorkflowAdvanceOutcome(
            handled=handled,
            reason=reason,
            extra=dict(extra or {}),
            requires_persisted_execution=handled if requires_persisted_execution is None else requires_persisted_execution,
            failed=failed,
        )

    @workflow_step(
        key=PARENT_OP_NOTIFICATION_EMIT,
        label="Notification emit",
        kind=WorkflowStepKind.NOTIFICATION,
        after=PARENT_OP_BRIEF_NORMALIZATION,
        supports=(PARENT_OP_BACKLOG_PLANNING, PARENT_OP_JIRA_CHILD_FANOUT),
        required=False,
        description="Emit user/admin notifications tied to workflow failures or remediation.",
    )
    def _notification_emit_step_contract(self) -> None:
        raise NotImplementedError

    @workflow_step(
        key=PARENT_OP_JIRA_PARENT_UPDATE,
        label="Jira parent update",
        kind=WorkflowStepKind.INTEGRATION,
        after=PARENT_OP_BRIEF_NORMALIZATION,
        supports=(PARENT_OP_BACKLOG_PLANNING, PARENT_OP_JIRA_CHILD_FANOUT),
        required=False,
        retryable=True,
        description="Synchronize parent Jira issue metadata, labels, and architecture references.",
    )
    def _update_issue_sync_label_step(
        self,
        *,
        lifecycle,
        issue_gateway,
        parent_detail,
        target_label: str,
    ) -> None:
        step = start_workflow_step_attempt(lifecycle=lifecycle, operation_type=PARENT_OP_JIRA_PARENT_UPDATE)
        try:
            issue_gateway.update_issue_sync_label(
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

    def _mark_issues_sync_blocked_step(
        self,
        *,
        lifecycle,
        issue_gateway,
        issue_keys: list[str],
    ) -> None:
        step = start_workflow_step_attempt(lifecycle=lifecycle, operation_type=PARENT_OP_JIRA_PARENT_UPDATE)
        try:
            issue_gateway.mark_issues_sync_blocked(issue_keys=issue_keys)
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

    def _handle_issue_created(self, *, context, session: Session, settings, lifecycle) -> WorkflowAdvanceOutcome:  # noqa: ANN001
        issue_gateway = self._deps.issue_gateway
        parent_detail = issue_gateway.load_parent_detail(context.issue_key)
        lifecycle.ensure_execution(
            display_name=parent_detail.summary,
            description=parent_detail.description,
        )
        project_key = self._deps.project_key_for_issue_fn(context.issue_key)
        product_brief, normalization_questions = self._resolve_product_brief_step(
            lifecycle=lifecycle,
            brief_planner=self._deps.brief_planner,
            parent_detail=parent_detail,
            refresh=False,
        )
        architecture_gate = issue_gateway.resolve_architecture_gate(
            parent_issue_key=context.issue_key,
            issue_summary=parent_detail.summary,
            issue_labels=list(parent_detail.labels or []),
        )
        self._sync_parent_issue_references_step(
            lifecycle=lifecycle,
            issue_gateway=issue_gateway,
            parent_detail=parent_detail,
            architecture_gate=architecture_gate,
            draft=bool(normalization_questions) or bool(architecture_gate.required and not architecture_gate.ready),
        )
        if normalization_questions:
            return self._block_parent_brief(
                context=context,
                session=session,
                settings=settings,
                parent_detail=parent_detail,
                lifecycle=lifecycle,
                questions=normalization_questions,
                body_prefix="Parent feature was created in backlog, but brief normalization is blocked pending clarification.",
                reason="pm_parent_issue_created_brief_blocked",
            )
        if architecture_gate.required and not architecture_gate.ready:
            return self._block_on_architecture(
                context=context,
                issue_gateway=issue_gateway,
                parent_detail=parent_detail,
                lifecycle=lifecycle,
                reason="pm_parent_issue_created_architecture_blocked",
                extra={
                    "architecture_document_title": architecture_gate.document.title if architecture_gate.document else None,
                    "architecture_document_url": (
                        str(architecture_gate.document.canonical_url or "").strip()
                        if architecture_gate.document is not None
                        else None
                    ),
                },
            )
        try:
            fanout = self._plan_and_seed_with_attempts(
                lifecycle=lifecycle,
                parent_detail=parent_detail,
                product_brief=product_brief,
                project_key=project_key,
                planning_summary="Backlog planning completed from the normalized parent brief.",
                fanout_summary="Engineering child tickets were created or refreshed from the parent planning package.",
            )
        except ParentPlanningFanoutSeedError as exc:
            return self._handle_fanout_failure(
                context=context,
                issue_gateway=issue_gateway,
                lifecycle=lifecycle,
                error=exc.error,
                failure_reason="pm_parent_issue_created_seed_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._handle_fanout_failure(
                context=context,
                issue_gateway=issue_gateway,
                lifecycle=lifecycle,
                error=exc,
                failure_reason="pm_parent_issue_created_seed_failed",
            )

        if not fanout.completed:
            return self._outcome(
                handled=True,
                reason="pm_parent_issue_created_seed_blocked",
                extra={
                    "questions": ClarificationQuestionSet(questions=fanout.questions).to_payload(),
                    "parent_revision": fanout.seed_data.get("parent_revision"),
                    "children_sync_status": fanout.seed_data.get("children_sync_status"),
                    "webhook_event": context.webhook_event,
                },
            )

        return self._outcome(
            handled=True,
            reason="pm_parent_issue_created_seed_completed",
            extra={
                "updated_children": fanout.changed_children,
                "parent_revision": fanout.seed_data.get("parent_revision"),
                "children_sync_status": fanout.seed_data.get("children_sync_status"),
                "webhook_event": context.webhook_event,
            },
        )

    def _handle_issue_updated(self, *, context, session: Session, settings, lifecycle) -> WorkflowAdvanceOutcome:  # noqa: ANN001
        issue_gateway = self._deps.issue_gateway
        material_changed_fields = self._deps.material_parent_changed_fields_fn(
            payload=context.payload,
            extract_changed_fields_fn=self._deps.extract_changed_fields_fn,
        )
        if not lifecycle.has_execution():
            return self._handle_issue_created(context=context, session=session, settings=settings, lifecycle=lifecycle)
        if not material_changed_fields:
            board_entry_target_status = self._deps.parent_board_entry_target_status_fn(
                payload=context.payload,
                extract_status_transition_fn=self._deps.extract_status_transition_fn,
            )
            if board_entry_target_status:
                return self._handle_board_entry(
                    context=context,
                    session=session,
                    target_status=board_entry_target_status,
                    lifecycle=lifecycle,
                )
            return self._outcome(
                handled=True,
                reason="pm_parent_non_material_change",
                extra={"changed_fields": [], "webhook_event": context.webhook_event},
                requires_persisted_execution=False,
            )

        parent_detail = issue_gateway.load_parent_detail(context.issue_key)
        lifecycle.ensure_execution(
            display_name=parent_detail.summary,
            description=parent_detail.description,
        )
        project_key = self._deps.project_key_for_issue_fn(context.issue_key)
        child_details = issue_gateway.load_child_details(
            project_key=project_key,
            parent_issue_key=context.issue_key,
        )
        product_brief, normalization_questions = self._resolve_product_brief_step(
            lifecycle=lifecycle,
            brief_planner=self._deps.brief_planner,
            parent_detail=parent_detail,
            refresh=True,
        )
        _ = product_brief
        architecture_gate = issue_gateway.resolve_architecture_gate(
            parent_issue_key=context.issue_key,
            issue_summary=parent_detail.summary,
            issue_labels=list(parent_detail.labels or []),
        )
        self._sync_parent_issue_references_step(
            lifecycle=lifecycle,
            issue_gateway=issue_gateway,
            parent_detail=parent_detail,
            architecture_gate=architecture_gate,
            draft=bool(normalization_questions) or bool(architecture_gate.required and not architecture_gate.ready),
        )
        if normalization_questions:
            blocked_issue_keys = [context.issue_key, *[detail.key for detail in child_details]]
            self._mark_issues_sync_blocked_step(
                lifecycle=lifecycle,
                issue_gateway=issue_gateway,
                issue_keys=blocked_issue_keys,
            )
            return self._block_parent_brief(
                context=context,
                session=session,
                settings=settings,
                parent_detail=parent_detail,
                lifecycle=lifecycle,
                questions=normalization_questions,
                body_prefix="Parent feature changed but brief normalization is blocked pending clarification.",
                reason="pm_parent_sync_brief_blocked",
                extra={"changed_fields": material_changed_fields},
            )
        if architecture_gate.required and not architecture_gate.ready:
            blocked_issue_keys = [context.issue_key, *[detail.key for detail in child_details]]
            self._mark_issues_sync_blocked_step(
                lifecycle=lifecycle,
                issue_gateway=issue_gateway,
                issue_keys=blocked_issue_keys,
            )
            return self._block_on_architecture(
                context=context,
                issue_gateway=issue_gateway,
                parent_detail=parent_detail,
                lifecycle=lifecycle,
                reason="pm_parent_sync_architecture_blocked",
                extra={
                    "changed_fields": material_changed_fields,
                    "architecture_document_title": architecture_gate.document.title if architecture_gate.document else None,
                    "architecture_document_url": (
                        str(architecture_gate.document.canonical_url or "").strip()
                        if architecture_gate.document is not None
                        else None
                    ),
                },
            )

        if not child_details:
            lifecycle.mark_completed_if_ready()
            return self._outcome(
                handled=True,
                reason="pm_parent_no_children",
                extra={"changed_fields": material_changed_fields, "webhook_event": context.webhook_event},
            )

        fanout_step = start_workflow_step_attempt(lifecycle=lifecycle, operation_type=PARENT_OP_JIRA_CHILD_FANOUT)
        fanout_gateway = self._deps.child_sync_gateway.with_attempt(
            attempt_ref=fanout_step.ref,
        )
        try:
            seed_data = fanout_gateway.refresh_parent_children(
                parent_detail=parent_detail,
                child_details=child_details,
                changed_fields=material_changed_fields,
                project_key=project_key,
            )
        except Exception as exc:  # noqa: BLE001
            category = classify_external_workflow_failure(error=exc)
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=fanout_step,
                category=category,
                message=str(exc),
            )
            blocked_issue_keys = [context.issue_key, *[detail.key for detail in child_details]]
            self._mark_issues_sync_blocked_step(
                lifecycle=lifecycle,
                issue_gateway=issue_gateway,
                issue_keys=blocked_issue_keys,
            )
            return self._outcome(
                handled=True,
                reason="pm_parent_sync_failed",
                extra={
                    "changed_fields": material_changed_fields,
                    "stale_child_keys": [detail.key for detail in child_details],
                    "webhook_event": context.webhook_event,
                },
                failed=True,
            )

        seed_evaluation = self._deps.fanout_service.evaluate_seed_data(
            seed_data=seed_data,
            combine_child_updates_fn=self._deps.child_sync_gateway.combined_child_updates,
        )
        if not seed_evaluation.completed:
            blocked_issue_keys = [context.issue_key, *[detail.key for detail in child_details]]
            self._mark_issues_sync_blocked_step(
                lifecycle=lifecycle,
                issue_gateway=issue_gateway,
                issue_keys=blocked_issue_keys,
            )
            self._publish_clarification_then_wait_step(
                lifecycle=lifecycle,
                blocking_step=fanout_step,
                parent_detail=parent_detail,
                questions=seed_evaluation.questions,
                context="Engineering child refresh",
            )
            return self._outcome(
                handled=True,
                reason="pm_parent_sync_blocked",
                extra={
                    "changed_fields": material_changed_fields,
                    "stale_child_keys": [detail.key for detail in child_details],
                    "questions": ClarificationQuestionSet(questions=seed_evaluation.questions).to_payload(),
                    "webhook_event": context.webhook_event,
                },
            )
        complete_workflow_step_attempt(
            lifecycle=lifecycle,
            step=fanout_step,
            summary="Engineering child tickets were refreshed from the parent planning package.",
        )

        return self._outcome(
            handled=True,
            reason="pm_parent_sync_completed",
            extra={
                "changed_fields": material_changed_fields,
                "updated_children": seed_evaluation.changed_children,
                "parent_revision": seed_evaluation.seed_data.get("parent_revision"),
                "children_sync_status": seed_evaluation.seed_data.get("children_sync_status"),
                "webhook_event": context.webhook_event,
            },
        )

    @workflow_step(
        key=PARENT_OP_JIRA_CHILD_PROMOTION,
        label="Child promotion",
        kind=WorkflowStepKind.INTEGRATION,
        after=PARENT_OP_JIRA_CHILD_FANOUT,
        supports=PARENT_OP_JIRA_CHILD_FANOUT,
        required=False,
        description="Promote backlog engineering child tickets onto the working board.",
    )
    def _handle_board_entry(
        self,
        *,
        context,
        session: Session,
        target_status: str,
        lifecycle,
    ) -> WorkflowAdvanceOutcome:
        issue_gateway = self._deps.issue_gateway
        parent_detail = issue_gateway.load_parent_detail(context.issue_key)
        lifecycle.ensure_execution(
            display_name=parent_detail.summary,
            description=parent_detail.description,
        )
        project_key = self._deps.project_key_for_issue_fn(context.issue_key)
        child_details = issue_gateway.load_child_details(project_key=project_key, parent_issue_key=context.issue_key)
        promotion_step = start_workflow_step_attempt(lifecycle=lifecycle, operation_type=PARENT_OP_JIRA_CHILD_PROMOTION)
        if not child_details:
            complete_workflow_step_attempt(
                lifecycle=lifecycle,
                step=promotion_step,
                summary=f"No engineering child tickets required promotion to {target_status}.",
            )
            lifecycle.mark_completed_if_ready()
            return self._outcome(
                handled=True,
                reason="pm_parent_board_entry_no_children",
                extra={"target_status": target_status, "webhook_event": context.webhook_event},
            )

        promoted_children: list[str] = []
        unchanged_children: list[str] = []
        skipped_children: list[str] = []
        failed_children: list[str] = []
        for child_detail in child_details:
            child_labels = {str(label).strip().casefold() for label in child_detail.labels}
            if "engineering-child" not in child_labels:
                skipped_children.append(child_detail.key)
                continue
            if str(child_detail.status or "").strip().casefold() in {"to do", "ready for agent", "in progress", "testing", "done"}:
                unchanged_children.append(child_detail.key)
                continue
            try:
                issue_gateway.transition_issue(issue_key=child_detail.key, target_status=target_status)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "jira_parent_board_entry_child_transition_failed request_id=%s tenant_id=%s parent_issue_key=%s child_issue_key=%s target_status=%s error=%s",
                    context.request_id,
                    context.tenant_id,
                    context.issue_key,
                    child_detail.key,
                    target_status,
                    exc,
                )
                failed_children.append(child_detail.key)
                continue
            promoted_children.append(child_detail.key)

        if failed_children:
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=promotion_step,
                category="external_failure",
                message=(
                    f"Failed to promote engineering child tickets to {target_status}: "
                    f"{', '.join(failed_children)}"
                ),
            )
        else:
            complete_workflow_step_attempt(
                lifecycle=lifecycle,
                step=promotion_step,
                summary=(
                    f"Promoted engineering child tickets to {target_status}: "
                    f"{', '.join(promoted_children)}."
                    if promoted_children
                    else f"No engineering child tickets required promotion to {target_status}."
                ),
            )
            lifecycle.mark_completed_if_ready()

        return self._outcome(
            handled=True,
            reason="pm_parent_board_entry_fanout_completed" if not failed_children else "pm_parent_board_entry_fanout_partial",
            extra={
                "target_status": target_status,
                "promoted_children": promoted_children,
                "unchanged_children": unchanged_children,
                "skipped_children": skipped_children,
                "failed_children": failed_children,
                "webhook_event": context.webhook_event,
            },
        )

    def _block_parent_brief(
        self,
        *,
        context,
        session: Session,
        settings,  # noqa: ANN001
        parent_detail,
        lifecycle,
        questions: tuple[ClarificationQuestion, ...],
        body_prefix: str,
        reason: str,
        extra: dict[str, object] | None = None,
    ) -> WorkflowAdvanceOutcome:
        _ = (session, settings, body_prefix)
        question_set = ClarificationQuestionSet(questions=questions)
        required_questions = self._deps.clarification_service.require_questions_for_waiting_state(
            questions=question_set.questions,
            context="Parent planning",
        )
        issue_gateway = self._deps.issue_gateway
        self._update_issue_sync_label_step(
            lifecycle=lifecycle,
            issue_gateway=issue_gateway,
            parent_detail=parent_detail,
            target_label="sync-blocked",
        )
        self._ensure_clarification_with_projection_attempts(
            lifecycle=lifecycle,
            issue_key=parent_detail.key,
            questions=required_questions,
            publisher=issue_gateway,
        )
        lifecycle.mark_workflow_waiting_for_input()
        payload = dict(extra or {})
        payload.update(
            {
                "questions": ClarificationQuestionSet(questions=required_questions).to_payload(),
                "webhook_event": context.webhook_event,
            }
        )
        return self._outcome(
            handled=True,
            reason=reason,
            extra=payload,
        )

    def _block_on_architecture(
        self,
        *,
        context,
        issue_gateway,
        parent_detail,
        lifecycle,
        reason: str,
        extra: dict[str, object] | None = None,
    ) -> WorkflowAdvanceOutcome:
        self._update_issue_sync_label_step(
            lifecycle=lifecycle,
            issue_gateway=issue_gateway,
            parent_detail=parent_detail,
            target_label="sync-blocked",
        )
        lifecycle.mark_workflow_waiting_for_input()
        payload = dict(extra or {})
        payload["webhook_event"] = context.webhook_event
        return self._outcome(
            handled=True,
            reason=reason,
            extra=payload,
        )

    def _handle_pm_interview_followup(
        self,
        *,
        context,
        session: Session,
        settings,  # noqa: ANN001
        lifecycle,
    ) -> WorkflowAdvanceOutcome:
        issue_gateway = self._deps.issue_gateway
        parent_detail = issue_gateway.load_parent_detail(context.issue_key)
        lifecycle.ensure_execution(
            display_name=parent_detail.summary,
            description=parent_detail.description,
        )
        project_key = self._deps.project_key_for_issue_fn(context.issue_key)
        brief_payload = dict(context.payload.get("brief_payload") or {})
        next_questions = ClarificationQuestionSet.from_values(context.payload.get("next_questions", [])).questions
        ready_to_write = bool(context.payload.get("ready_to_write"))

        if not ready_to_write:
            required_questions = self._deps.clarification_service.require_questions_for_waiting_state(
                questions=next_questions,
                context="PM interview follow-up",
            )
            self._update_issue_sync_label_step(
                lifecycle=lifecycle,
                issue_gateway=issue_gateway,
                parent_detail=parent_detail,
                target_label="sync-blocked",
            )
            publication = self._ensure_clarification_with_projection_attempts(
                lifecycle=lifecycle,
                issue_key=context.issue_key,
                questions=required_questions,
                publisher=issue_gateway,
            )
            lifecycle.mark_workflow_waiting_for_input()
            return self._outcome(
                handled=True,
                reason="pm_interview_still_open",
                extra={
                    "questions": ClarificationQuestionSet(questions=required_questions).to_payload(),
                    "comment_posted": publication.jira_comment_created,
                    "webhook_event": context.webhook_event,
                },
            )

        self._complete_brief_normalization_from_followup(
            lifecycle=lifecycle,
        )
        architecture_gate = issue_gateway.resolve_architecture_gate(
            parent_issue_key=context.issue_key,
            issue_summary=parent_detail.summary,
            issue_labels=list(parent_detail.labels or []),
        )
        self._sync_parent_issue_references_step(
            lifecycle=lifecycle,
            issue_gateway=issue_gateway,
            parent_detail=parent_detail,
            architecture_gate=architecture_gate,
            draft=bool(architecture_gate.required and not architecture_gate.ready),
        )
        if architecture_gate.required and not architecture_gate.ready:
            return self._block_on_architecture(
                context=context,
                issue_gateway=issue_gateway,
                parent_detail=parent_detail,
                lifecycle=lifecycle,
                reason="pm_interview_followup_architecture_blocked",
                extra={
                    "architecture_document_title": architecture_gate.document.title if architecture_gate.document else None,
                    "architecture_document_url": (
                        str(architecture_gate.document.canonical_url or "").strip()
                        if architecture_gate.document is not None
                        else None
                    ),
                },
            )

        try:
            fanout = self._plan_and_seed_with_attempts(
                lifecycle=lifecycle,
                parent_detail=parent_detail,
                product_brief=brief_payload,
                project_key=project_key,
                planning_summary="Backlog planning completed from the confirmed parent brief.",
                fanout_summary="Engineering child tickets were created or refreshed from the confirmed brief.",
            )
        except ParentPlanningFanoutSeedError as exc:
            return self._handle_fanout_failure(
                context=context,
                issue_gateway=issue_gateway,
                lifecycle=lifecycle,
                error=exc.error,
                failure_reason="pm_interview_followup_seed_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._handle_fanout_failure(
                context=context,
                issue_gateway=issue_gateway,
                lifecycle=lifecycle,
                error=exc,
                failure_reason="pm_interview_followup_seed_failed",
            )

        if not fanout.completed:
            return self._outcome(
                handled=True,
                reason="pm_interview_followup_planning_blocked",
                extra={
                    "questions": ClarificationQuestionSet(questions=fanout.questions).to_payload(),
                    "parent_revision": fanout.seed_data.get("parent_revision"),
                    "children_sync_status": fanout.seed_data.get("children_sync_status"),
                    "webhook_event": context.webhook_event,
                },
            )

        return self._outcome(
            handled=True,
            reason="pm_interview_followup_resolved",
            extra={
                "updated_children": fanout.changed_children,
                "parent_revision": fanout.seed_data.get("parent_revision"),
                "children_sync_status": fanout.seed_data.get("children_sync_status"),
                "webhook_event": context.webhook_event,
            },
        )

    def _complete_brief_normalization_from_followup(self, *, lifecycle) -> None:  # noqa: ANN001
        step = start_workflow_step_attempt(lifecycle=lifecycle, operation_type=PARENT_OP_BRIEF_NORMALIZATION)
        complete_workflow_step_attempt(
            lifecycle=lifecycle,
            step=step,
            summary="Parent brief normalized from product clarification.",
        )

    def _sync_parent_issue_references(
        self,
        *,
        issue_gateway,
        parent_detail,
        architecture_gate,
    ) -> None:
        issue_gateway.upsert_workflow_execution_link(issue_key=parent_detail.key)
        architecture_document = getattr(architecture_gate, "document", None)
        if architecture_document is None:
            return
        title = str(getattr(architecture_document, "title", "") or "").strip()
        url = str(getattr(architecture_document, "canonical_url", "") or "").strip()
        if not title or not url:
            raise ValueError(f"Architecture document link is incomplete for {parent_detail.key}")
        issue_gateway.upsert_architecture_document_link(
            issue_key=parent_detail.key,
            title=title,
            url=url,
        )

    @workflow_step(
        key=PARENT_OP_BRIEF_NORMALIZATION,
        label="PM brief",
        kind=WorkflowStepKind.HUMAN_GATE,
        required=True,
        description="Normalize the parent issue into the product brief or pause for clarification.",
    )
    def _resolve_product_brief_step(
        self,
        *,
        lifecycle,
        brief_planner,
        parent_detail,
        refresh: bool,
    ) -> tuple[dict[str, object], tuple[ClarificationQuestion, ...]]:
        step = start_workflow_step_attempt(lifecycle=lifecycle, operation_type=PARENT_OP_BRIEF_NORMALIZATION)
        planner = brief_planner.with_attempt(
            attempt_ref=step.ref,
        )
        try:
            product_brief, raw_questions = planner.resolve_product_brief(
                parent_detail=parent_detail,
                refresh=refresh,
            )
        except Exception as exc:  # noqa: BLE001
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=step,
                category=classify_external_workflow_failure(error=exc),
                message=str(exc),
            )
            raise
        questions = ClarificationQuestionSet.from_values(raw_questions).questions
        if questions:
            wait_workflow_step_attempt(
                lifecycle=lifecycle,
                step=step,
                summary="Parent planning is waiting for product clarification.",
            )
        else:
            complete_workflow_step_attempt(
                lifecycle=lifecycle,
                step=step,
                summary="Parent brief normalized from the source issue.",
            )
        return product_brief, questions

    def _sync_parent_issue_references_step(
        self,
        *,
        lifecycle,
        issue_gateway,
        parent_detail,
        architecture_gate,
        draft: bool,
    ) -> None:
        step = start_workflow_step_attempt(lifecycle=lifecycle, operation_type=PARENT_OP_JIRA_PARENT_UPDATE)
        try:
            self._sync_parent_issue_references(
                issue_gateway=issue_gateway,
                parent_detail=parent_detail,
                architecture_gate=architecture_gate,
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
            summary=(
                "Parent Jira issue metadata synced without modifying the source description."
                if draft
                else "Parent Jira issue metadata synced without modifying the source description."
            ),
        )

    @workflow_step(
        key=PARENT_OP_BACKLOG_PLANNING,
        label="Backlog planning",
        kind=WorkflowStepKind.BUSINESS,
        after=PARENT_OP_BRIEF_NORMALIZATION,
        required=True,
        retryable=True,
        description="Build the planning package that determines required engineering child tickets.",
    )
    @workflow_step(
        key=PARENT_OP_JIRA_CHILD_FANOUT,
        label="Engineering child fanout",
        kind=WorkflowStepKind.INTEGRATION,
        after=PARENT_OP_BACKLOG_PLANNING,
        required=True,
        retryable=True,
        description="Create or refresh the engineering child tickets implied by the confirmed parent brief.",
    )
    def _plan_and_seed_with_attempts(
        self,
        *,
        lifecycle,
        parent_detail,
        product_brief: dict[str, Any],
        project_key: str,
        planning_summary: str,
        fanout_summary: str,
    ) -> ParentPlanningFanoutResult:
        planning_step = start_workflow_step_attempt(lifecycle=lifecycle, operation_type=PARENT_OP_BACKLOG_PLANNING)
        planner = self._deps.brief_planner.with_attempt(
            attempt_ref=planning_step.ref,
        )
        try:
            planning_result, planning_package = planner.plan_backlog_parent(
                parent_detail=parent_detail,
                product_brief=product_brief,
                project_key=project_key,
            )
        except Exception as exc:  # noqa: BLE001
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=planning_step,
                category=classify_external_workflow_failure(error=exc),
                message=str(exc),
            )
            raise

        planning_questions = ClarificationQuestionSet.from_values(
            getattr(planning_result, "open_behavior_questions", ()) or ()
        ).questions
        if planning_result.planning_state == PLANNING_STATE_COMPLETED:
            complete_workflow_step_attempt(
                lifecycle=lifecycle,
                step=planning_step,
                summary=planning_summary,
            )
        else:
            self._publish_clarification_then_wait_step(
                lifecycle=lifecycle,
                blocking_step=planning_step,
                parent_detail=parent_detail,
                questions=planning_questions,
                context="Backlog planning",
            )
            return self._deps.fanout_service.blocked_planning_result(
                planning_result=planning_result,
                planning_package=planning_package,
            )

        fanout_step = start_workflow_step_attempt(lifecycle=lifecycle, operation_type=PARENT_OP_JIRA_CHILD_FANOUT)
        child_sync_gateway = self._deps.child_sync_gateway.with_attempt(
            attempt_ref=fanout_step.ref,
        )
        try:
            seed_data = child_sync_gateway.seed_parent_backlog_children(
                parent_detail=parent_detail,
                project_key=project_key,
                planning_package=planning_package,
                planning_state=planning_result.planning_state,
            )
        except Exception as exc:  # noqa: BLE001
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=fanout_step,
                category=classify_external_workflow_failure(error=exc),
                message=str(exc),
            )
            raise ParentPlanningFanoutSeedError(
                error=exc,
                planning_result=planning_result,
                planning_package=planning_package,
            ) from exc

        seed_evaluation = self._deps.fanout_service.evaluate_seed_data(
            seed_data=seed_data,
            combine_child_updates_fn=child_sync_gateway.combined_child_updates,
            planning_result=planning_result,
        )
        fanout = ParentPlanningFanoutResult(
            planning_result=planning_result,
            planning_package=planning_package,
            seed_evaluation=seed_evaluation,
        )
        if fanout.completed:
            complete_workflow_step_attempt(
                lifecycle=lifecycle,
                step=fanout_step,
                summary=fanout_summary,
            )
            lifecycle.mark_completed_if_ready()
        else:
            self._publish_clarification_then_wait_step(
                lifecycle=lifecycle,
                blocking_step=fanout_step,
                parent_detail=parent_detail,
                questions=fanout.questions,
                context="Engineering child fanout",
            )
        return fanout

    def _publish_clarification_then_wait_step(
        self,
        *,
        lifecycle,
        blocking_step,
        parent_detail,
        questions: tuple[ClarificationQuestion, ...],
        context: str,
    ) -> None:
        required_questions = self._deps.clarification_service.require_questions_for_waiting_state(
            questions=questions,
            context=context,
        )
        issue_gateway = self._deps.issue_gateway
        try:
            self._update_issue_sync_label_step(
                lifecycle=lifecycle,
                issue_gateway=issue_gateway,
                parent_detail=parent_detail,
                target_label="sync-blocked",
            )
            publication = self._ensure_clarification_with_projection_attempts(
                lifecycle=lifecycle,
                issue_key=parent_detail.key,
                questions=required_questions,
                publisher=issue_gateway,
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
            summary=self._deps.clarification_service.build_missing_input_message(
                issue_key=parent_detail.key,
                questions=required_questions,
            ),
        )

    @workflow_step(
        key=PARENT_OP_DISCORD_FOLLOWUP_PROJECTION,
        label="Discord follow-up projection",
        kind=WorkflowStepKind.NOTIFICATION,
        after=PARENT_OP_BRIEF_NORMALIZATION,
        supports=(PARENT_OP_BACKLOG_PLANNING, PARENT_OP_JIRA_CHILD_FANOUT),
        required=False,
        description="Publish clarification follow-up state to Discord.",
    )
    @workflow_step(
        key=PARENT_OP_JIRA_COMMENT_PROJECTION,
        label="Jira comment projection",
        kind=WorkflowStepKind.NOTIFICATION,
        after=PARENT_OP_BRIEF_NORMALIZATION,
        supports=(PARENT_OP_BACKLOG_PLANNING, PARENT_OP_JIRA_CHILD_FANOUT),
        required=False,
        description="Publish clarification questions to Jira.",
    )
    def _ensure_clarification_with_projection_attempts(
        self,
        *,
        lifecycle,
        issue_key: str,
        questions: tuple[ClarificationQuestion, ...],
        publisher,
    ):
        active_effects = publisher.active_clarification_effects(issue_key=issue_key, questions=questions)
        if active_effects is not None:
            if not active_effects.jira_comment_id:
                raise RuntimeError(f"Active clarification for {issue_key} has no persisted Jira comment id")
            return self._deps.clarification_service.ensure_active_clarification(
                issue_key=issue_key,
                questions=questions,
                publisher=publisher,
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
            publication = self._deps.clarification_service.ensure_active_clarification(
                issue_key=issue_key,
                questions=questions,
                publisher=publisher,
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

        missing_projection = False
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
        else:
            missing_projection = True
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=jira_step,
                category="contract_violation",
                message=f"Jira clarification projection did not run for {issue_key}",
            )
        if missing_projection:
            message = f"Clarification projection did not run for {issue_key}"
            raise RuntimeError(message)
        return publication

    def _handle_fanout_failure(
        self,
        *,
        context,
        issue_gateway,
        lifecycle,
        error: Exception,
        failure_reason: str,
    ) -> WorkflowAdvanceOutcome:
        logger.exception(
            "parent_planning_fanout_failed request_id=%s tenant_id=%s issue_key=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            error,
        )
        self._update_issue_sync_label_step(
            lifecycle=lifecycle,
            issue_gateway=issue_gateway,
            parent_detail=issue_gateway.load_parent_detail(context.issue_key),
            target_label="sync-blocked",
        )
        return self._outcome(
            handled=True,
            reason=failure_reason,
            extra={"error": str(error), "webhook_event": context.webhook_event},
            failed=True,
        )
