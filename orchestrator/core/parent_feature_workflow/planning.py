from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session
from sqlalchemy import select

from orchestrator.core.clarification.questions import ClarificationQuestion, ClarificationQuestionSet
from orchestrator.core.development.start_work import StartWorkUseCase
from orchestrator.core.development.start_work_links import StartWorkActionTokenClaims, build_start_work_action_url
from orchestrator.core.projects.parent_planning_clarification_service import (
    ParentPlanningClarificationService,
)
from orchestrator.core.projects.parent_planning_fanout_service import (
    ParentPlanningFanoutResult,
    ParentPlanningFanoutSeedError,
    ParentPlanningFanoutService,
)
from orchestrator.core.parent_feature_workflow.operations import (
    PARENT_OP_BACKLOG_PLANNING,
    PARENT_OP_BRIEF_NORMALIZATION,
    PARENT_OP_DEVELOPMENT_START,
    PARENT_OP_DEVELOPMENT_START_LINK_PROJECTION,
    PARENT_OP_DISCORD_FOLLOWUP_PROJECTION,
    PARENT_OP_JIRA_CHILD_FANOUT,
    PARENT_OP_JIRA_CHILD_PROMOTION,
    PARENT_OP_JIRA_COMMENT_PROJECTION,
    PARENT_OP_JIRA_PARENT_UPDATE,
    PARENT_OP_NOTIFICATION_EMIT,
    PARENT_OP_PM_DECISION_RESOLUTION,
    PARENT_WU_BACKLOG_ARCHITECTURE_MODEL,
    PARENT_WU_BACKLOG_PACKAGE_ASSEMBLY,
    PARENT_WU_BACKLOG_SECURITY_MODEL,
    PARENT_WU_BACKLOG_TESTING_MODEL,
    PARENT_WU_BRIEF_NORMALIZATION_MODEL,
    PARENT_WU_DISCORD_FOLLOWUP_PROJECTION_API,
    PARENT_WU_JIRA_CHILD_FANOUT_EVALUATE,
    PARENT_WU_JIRA_CHILD_FANOUT_ARCHITECTURE_MODEL,
    PARENT_WU_JIRA_CHILD_FANOUT_PACKAGE_ASSEMBLY,
    PARENT_WU_JIRA_CHILD_FANOUT_SECURITY_MODEL,
    PARENT_WU_JIRA_CHILD_FANOUT_SEED,
    PARENT_WU_JIRA_CHILD_FANOUT_TESTING_MODEL,
    PARENT_WU_JIRA_CHILD_PROMOTION_API,
    PARENT_WU_JIRA_COMMENT_PROJECTION_API,
    PARENT_WU_JIRA_PARENT_UPDATE_API,
    PARENT_WU_NOTIFICATION_EMIT,
    PARENT_WU_PM_DECISION_MODEL,
)
from orchestrator.core.planning.decision_records import PlanningDecisionRecordStore
from orchestrator.core.parent_feature_workflow.clarification_steps import ParentPlanningClarificationStepRunner
from orchestrator.core.parent_feature_workflow.child_fanout_execution import (
    ChildFanoutExecutionInput,
    execute_child_fanout_step,
)
from orchestrator.core.planning.specialist import PLANNING_STATE_COMPLETED
from orchestrator.core.workflow.advance import WorkflowAdvanceLifecycle, WorkflowAdvanceOutcome
from orchestrator.core.workflow.definition import (
    WorkflowStepKind,
    WorkflowWorkUnitIdempotencyPolicy,
    WorkflowWorkUnitKind,
    WorkflowWorkUnitRetryPolicy,
    workflow_step,
    workflow_work_unit,
)
from orchestrator.core.workflow.execution_projection import classify_external_workflow_failure
from orchestrator.core.workflow.execution_projection import resolve_latest_workflow_execution_by_source
from orchestrator.core.workflow.step_runner import (
    complete_workflow_step_attempt,
    fail_workflow_step_attempt,
    start_workflow_step_attempt,
    wait_workflow_step_attempt,
)
from orchestrator.core.workflow.work_units import run_work_unit, workflow_work_unit_input_fingerprint
from orchestrator.storage.models import Project, WorkflowOperation

logger = logging.getLogger(__name__)

_BOARD_ENTRY_STATUSES = {"to do", "ready for agent"}
_CHILD_ALREADY_ACTIONABLE_STATUSES = {"to do", "ready for agent", "in progress", "testing", "done"}


def _normalized_status(value: object) -> str:
    return str(value or "").strip().casefold()


def _promotion_target_for_parent_status(parent_status: object) -> str | None:
    normalized = _normalized_status(parent_status)
    if normalized not in _BOARD_ENTRY_STATUSES:
        return None
    return str(parent_status or "").strip() or None


def _stakeholder_escalation_questions(pm_resolution) -> tuple[ClarificationQuestion, ...]:  # noqa: ANN001
    questions: list[object] = []
    for escalation in getattr(pm_resolution, "stakeholder_escalations", ()) or ():
        to_question = getattr(escalation, "to_clarification_question", None)
        questions.append(to_question() if callable(to_question) else escalation)
    return ClarificationQuestionSet.from_values(questions).questions


def _augment_product_brief_with_pm_resolution(
    *,
    product_brief: dict[str, Any],
    pm_resolution,
) -> dict[str, Any]:
    augmented = dict(product_brief)
    planning_context = dict(augmented.get("planning_context") or {})
    pm_answers = [resolution.to_payload() for resolution in getattr(pm_resolution, "resolved_decisions", ()) or ()]
    if pm_answers:
        planning_context["pm_decision_resolutions"] = pm_answers
    updated_context = dict(getattr(pm_resolution, "updated_planning_context", {}) or {})
    if updated_context:
        planning_context.update(updated_context)
    if planning_context:
        augmented["planning_context"] = planning_context
    return augmented


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

    def _clarification_steps(self) -> ParentPlanningClarificationStepRunner:
        return ParentPlanningClarificationStepRunner(
            clarification_service=self._deps.clarification_service,
            issue_gateway=self._deps.issue_gateway,
        )

    @workflow_work_unit(
        key=PARENT_WU_BRIEF_NORMALIZATION_MODEL,
        step_key=PARENT_OP_BRIEF_NORMALIZATION,
        label="Normalize PM brief",
        kind=WorkflowWorkUnitKind.MODEL_CALL,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key=PARENT_WU_JIRA_PARENT_UPDATE_API,
        step_key=PARENT_OP_JIRA_PARENT_UPDATE,
        label="Update parent Jira issue",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key=PARENT_WU_BACKLOG_ARCHITECTURE_MODEL,
        step_key=PARENT_OP_BACKLOG_PLANNING,
        label="Architecture planning",
        kind=WorkflowWorkUnitKind.MODEL_CALL,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key=PARENT_WU_BACKLOG_SECURITY_MODEL,
        step_key=PARENT_OP_BACKLOG_PLANNING,
        label="Security planning",
        kind=WorkflowWorkUnitKind.MODEL_CALL,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key=PARENT_WU_BACKLOG_TESTING_MODEL,
        step_key=PARENT_OP_BACKLOG_PLANNING,
        label="Testing planning",
        kind=WorkflowWorkUnitKind.MODEL_CALL,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key=PARENT_WU_BACKLOG_PACKAGE_ASSEMBLY,
        step_key=PARENT_OP_BACKLOG_PLANNING,
        label="Assemble planning package",
        kind=WorkflowWorkUnitKind.ASSEMBLY,
        idempotency_policy=WorkflowWorkUnitIdempotencyPolicy(required=True),
    )
    @workflow_work_unit(
        key=PARENT_WU_PM_DECISION_MODEL,
        step_key=PARENT_OP_PM_DECISION_RESOLUTION,
        label="Resolve PM decisions",
        kind=WorkflowWorkUnitKind.MODEL_CALL,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key=PARENT_WU_JIRA_CHILD_FANOUT_SEED,
        step_key=PARENT_OP_JIRA_CHILD_FANOUT,
        label="Seed Jira child issues",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key=PARENT_WU_JIRA_CHILD_FANOUT_EVALUATE,
        step_key=PARENT_OP_JIRA_CHILD_FANOUT,
        label="Evaluate child fanout",
        kind=WorkflowWorkUnitKind.ASSEMBLY,
        idempotency_policy=WorkflowWorkUnitIdempotencyPolicy(required=True),
    )
    @workflow_work_unit(
        key=PARENT_WU_JIRA_CHILD_FANOUT_ARCHITECTURE_MODEL,
        step_key=PARENT_OP_JIRA_CHILD_FANOUT,
        label="Refresh architecture planning",
        kind=WorkflowWorkUnitKind.MODEL_CALL,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key=PARENT_WU_JIRA_CHILD_FANOUT_SECURITY_MODEL,
        step_key=PARENT_OP_JIRA_CHILD_FANOUT,
        label="Refresh security planning",
        kind=WorkflowWorkUnitKind.MODEL_CALL,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key=PARENT_WU_JIRA_CHILD_FANOUT_TESTING_MODEL,
        step_key=PARENT_OP_JIRA_CHILD_FANOUT,
        label="Refresh testing planning",
        kind=WorkflowWorkUnitKind.MODEL_CALL,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key=PARENT_WU_JIRA_CHILD_FANOUT_PACKAGE_ASSEMBLY,
        step_key=PARENT_OP_JIRA_CHILD_FANOUT,
        label="Refresh planning package",
        kind=WorkflowWorkUnitKind.ASSEMBLY,
        idempotency_policy=WorkflowWorkUnitIdempotencyPolicy(required=True),
    )
    @workflow_work_unit(
        key=PARENT_WU_JIRA_CHILD_PROMOTION_API,
        step_key=PARENT_OP_JIRA_CHILD_PROMOTION,
        label="Promote child issues",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key=PARENT_WU_JIRA_COMMENT_PROJECTION_API,
        step_key=PARENT_OP_JIRA_COMMENT_PROJECTION,
        label="Publish Jira clarification",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key=PARENT_WU_DISCORD_FOLLOWUP_PROJECTION_API,
        step_key=PARENT_OP_DISCORD_FOLLOWUP_PROJECTION,
        label="Publish Discord follow-up",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key=PARENT_WU_NOTIFICATION_EMIT,
        step_key=PARENT_OP_NOTIFICATION_EMIT,
        label="Emit notification",
        kind=WorkflowWorkUnitKind.SIDE_EFFECT,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    def _work_unit_contract(self) -> None:
        raise NotImplementedError

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
        _ = issue_gateway
        self._clarification_steps().update_sync_label(
            lifecycle=lifecycle,
            parent_detail=parent_detail,
            target_label=target_label,
        )

    def _mark_issues_sync_blocked_step(
        self,
        *,
        lifecycle,
        issue_gateway,
        issue_keys: list[str],
    ) -> None:
        _ = issue_gateway
        self._clarification_steps().mark_issues_sync_blocked(
            lifecycle=lifecycle,
            issue_keys=issue_keys,
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
                session=session,
                settings=settings,
                tenant_id=context.tenant_id,
                project_id=context.project_id,
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
        if not lifecycle.has_execution():
            return self._handle_issue_created(context=context, session=session, settings=settings, lifecycle=lifecycle)
        if not material_changed_fields:
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
        supports=(PARENT_OP_JIRA_CHILD_FANOUT,),
        required=True,
        retryable=False,
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
        promotion_step = start_workflow_step_attempt(lifecycle=lifecycle, operation_type=PARENT_OP_JIRA_CHILD_PROMOTION)
        try:
            result = self._promote_child_issues_work_unit(
                lifecycle=lifecycle,
                step=promotion_step,
                issue_gateway=issue_gateway,
                parent_issue_key=context.issue_key,
                target_status=target_status,
                idempotency_context="board_entry",
            )
        except Exception as exc:  # noqa: BLE001
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=promotion_step,
                category=classify_external_workflow_failure(error=exc),
                message=str(exc),
            )
            raise

        promoted_children = list(result["promoted_children"])
        unchanged_children = list(result["unchanged_children"])
        skipped_children = list(result["skipped_children"])
        complete_workflow_step_attempt(
            lifecycle=lifecycle,
            step=promotion_step,
            summary=self._promotion_summary(
                target_status=target_status,
                promoted_children=promoted_children,
            ),
        )
        lifecycle.mark_completed_if_ready()
        tenant = context.tenant
        project = lifecycle.session.get(Project, context.project_id) if context.project_id else None
        if project is None:
            raise RuntimeError(f"Project is required to start development for {context.issue_key}")
        source_workflow = resolve_latest_workflow_execution_by_source(
            session=lifecycle.session,
            tenant_id=context.tenant_id,
            source_system="jira",
            source_ref=context.issue_key,
        )
        if source_workflow is None:
            raise RuntimeError(f"Parent planning workflow is required to start development for {context.issue_key}")
        StartWorkUseCase(session=lifecycle.session, issue_gateway=issue_gateway).start(
            tenant=tenant,
            project=project,
            issue_key=context.issue_key,
            target_status=target_status,
            actor="jira_board_transition",
            reason="parent_board_entry",
            source_workflow_id=source_workflow.workflow_id,
            require_source_workflow_completed=False,
            promote_targets=False,
        )

        return self._outcome(
            handled=True,
            reason="pm_parent_board_entry_fanout_completed",
            extra={
                "target_status": target_status,
                "promoted_children": promoted_children,
                "unchanged_children": unchanged_children,
                "skipped_children": skipped_children,
                "failed_children": [],
                "webhook_event": context.webhook_event,
            },
        )

    def _promote_child_issues_work_unit(
        self,
        *,
        lifecycle,
        step,
        issue_gateway,
        parent_issue_key: str,
        target_status: str,
        idempotency_context: str,
    ) -> dict[str, list[str]]:
        project_key = self._deps.project_key_for_issue_fn(parent_issue_key)
        input_payload = {
            "parent_issue_key": parent_issue_key,
            "project_key": project_key,
            "target_status": target_status,
            "idempotency_context": idempotency_context,
        }
        input_hash = workflow_work_unit_input_fingerprint(input_payload)
        return run_work_unit(
            lifecycle.session,
            operation=step.operation,
            operation_attempt=step.attempt,
            unit_key=PARENT_WU_JIRA_CHILD_PROMOTION_API,
            idempotency_key=f"{parent_issue_key}:jira_child_promotion:{input_hash}",
            input_payload=input_payload,
            execute=lambda _context: self._promote_child_issues(
                issue_gateway=issue_gateway,
                project_key=project_key,
                parent_issue_key=parent_issue_key,
                target_status=target_status,
            ),
            serialize=lambda result: dict(result),
            deserialize=lambda payload: {
                "promoted_children": list(payload.get("promoted_children") or []),
                "unchanged_children": list(payload.get("unchanged_children") or []),
                "skipped_children": list(payload.get("skipped_children") or []),
            },
        )

    def _promote_child_issues(
        self,
        *,
        issue_gateway,
        project_key: str,
        parent_issue_key: str,
        target_status: str,
    ) -> dict[str, list[str]]:
        child_details = issue_gateway.load_child_details(project_key=project_key, parent_issue_key=parent_issue_key)
        promoted_children: list[str] = []
        unchanged_children: list[str] = []
        skipped_children: list[str] = []
        for child_detail in child_details:
            child_labels = {str(label).strip().casefold() for label in child_detail.labels}
            if "engineering-child" not in child_labels:
                skipped_children.append(child_detail.key)
                continue
            if _normalized_status(child_detail.status) in _CHILD_ALREADY_ACTIONABLE_STATUSES:
                unchanged_children.append(child_detail.key)
                continue
            issue_gateway.transition_issue(issue_key=child_detail.key, target_status=target_status)
            promoted_children.append(child_detail.key)
        return {
            "promoted_children": promoted_children,
            "unchanged_children": unchanged_children,
            "skipped_children": skipped_children,
        }

    @staticmethod
    def _promotion_summary(*, target_status: str, promoted_children: list[str]) -> str:
        if promoted_children:
            return f"Promoted engineering child tickets to {target_status}: {', '.join(promoted_children)}."
        return f"No engineering child tickets required promotion to {target_status}."

    @workflow_step(
        key=PARENT_OP_DEVELOPMENT_START_LINK_PROJECTION,
        label="Development start link",
        kind=WorkflowStepKind.NOTIFICATION,
        after=PARENT_OP_JIRA_CHILD_PROMOTION,
        required=False,
        retryable=False,
        description="Publish the Jira action link that starts ready engineering work.",
    )
    def _development_start_link_projection_definition(self) -> None:
        raise NotImplementedError

    @workflow_step(
        key=PARENT_OP_DEVELOPMENT_START,
        label="Development start",
        kind=WorkflowStepKind.INTEGRATION,
        after=PARENT_OP_JIRA_CHILD_PROMOTION,
        required=False,
        retryable=False,
        description="Start executable development runs for engineering child tickets.",
    )
    def _development_start_definition(self) -> None:
        raise NotImplementedError

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
                session=session,
                settings=settings,
                tenant_id=context.tenant_id,
                project_id=context.project_id,
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
        brief_input = {
            "parent_issue_key": parent_detail.key,
            "summary": parent_detail.summary,
            "description": parent_detail.description,
            "refresh": bool(refresh),
        }
        brief_input_hash = workflow_work_unit_input_fingerprint(brief_input)
        try:
            product_brief, raw_questions = run_work_unit(
                session=lifecycle.session,
                operation=step.operation,
                operation_attempt=step.attempt,
                unit_key=PARENT_WU_BRIEF_NORMALIZATION_MODEL,
                idempotency_key=f"{parent_detail.key}:brief_normalization:{brief_input_hash}",
                input_payload=brief_input,
                execute=lambda _context: planner.resolve_product_brief(
                    parent_detail=parent_detail,
                    refresh=refresh,
                ),
                serialize=lambda result: {
                    "product_brief": result[0],
                    "questions": ClarificationQuestionSet.from_values(result[1]).to_payload(),
                },
                deserialize=lambda payload: (
                    dict(payload.get("product_brief") or {}),
                    list(payload.get("questions") or []),
                ),
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
        sync_input = {
            "parent_issue_key": parent_detail.key,
            "architecture_ready": bool(getattr(architecture_gate, "ready", False)),
            "draft": bool(draft),
        }
        sync_input_hash = workflow_work_unit_input_fingerprint(sync_input)
        try:
            run_work_unit(
                lifecycle.session,
                operation=step.operation,
                operation_attempt=step.attempt,
                unit_key=PARENT_WU_JIRA_PARENT_UPDATE_API,
                idempotency_key=f"{parent_detail.key}:jira_parent_update:{sync_input_hash}",
                input_payload=sync_input,
                execute=lambda _context: self._sync_parent_issue_references(
                    issue_gateway=issue_gateway,
                    parent_detail=parent_detail,
                    architecture_gate=architecture_gate,
                ),
                serialize=lambda _result: {"completed": True},
                deserialize=lambda _payload: None,
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
        key=PARENT_OP_PM_DECISION_RESOLUTION,
        label="PM decision resolution",
        kind=WorkflowStepKind.HUMAN_GATE,
        after=PARENT_OP_BACKLOG_PLANNING,
        supports=(PARENT_OP_BACKLOG_PLANNING, PARENT_OP_JIRA_CHILD_FANOUT),
        required=False,
        retryable=False,
        description="Resolve specialist product decision requests before any stakeholder clarification is published.",
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
        session: Session,
        settings,  # noqa: ANN001
        tenant_id: str,
        project_id: str | None,
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

        decision_store = PlanningDecisionRecordStore(session=session)
        for stage in getattr(planning_result, "stages", ()) or ():
            decision_store.record_technical_decisions(
                tenant_id=tenant_id,
                project_id=project_id,
                workflow_id=planning_step.ref.require_workflow_id(),
                source_operation_id=planning_step.ref.require_operation_id(),
                source_attempt_id=planning_step.ref.require_attempt_id(),
                parent_issue_key=parent_detail.key,
                source_stage=str(getattr(stage, "planning_state", "") or "").strip() or None,
                decisions=tuple(getattr(stage, "technical_decisions", ()) or ()),
            )
            decision_store.record_pm_requests(
                tenant_id=tenant_id,
                project_id=project_id,
                workflow_id=planning_step.ref.require_workflow_id(),
                source_operation_id=planning_step.ref.require_operation_id(),
                source_attempt_id=planning_step.ref.require_attempt_id(),
                parent_issue_key=parent_detail.key,
                source_stage=str(getattr(stage, "planning_state", "") or "").strip() or None,
                requests=tuple(getattr(stage, "pm_decision_requests", ()) or ()),
            )

        pm_decision_requests = tuple(getattr(planning_result, "pm_decision_requests", ()) or ())
        if pm_decision_requests:
            pm_step = start_workflow_step_attempt(lifecycle=lifecycle, operation_type=PARENT_OP_PM_DECISION_RESOLUTION)
            pm_planner = self._deps.brief_planner.with_attempt(attempt_ref=pm_step.ref)
            try:
                pm_resolution = pm_planner.resolve_pm_decisions(
                    parent_detail=parent_detail,
                    product_brief=product_brief,
                    planning_result=planning_result,
                    planning_package=planning_package,
                )
                decision_store.record_pm_resolutions(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    workflow_id=pm_step.ref.require_workflow_id(),
                    source_operation_id=pm_step.ref.require_operation_id(),
                    source_attempt_id=pm_step.ref.require_attempt_id(),
                    parent_issue_key=parent_detail.key,
                    resolutions=tuple(getattr(pm_resolution, "resolved_decisions", ()) or ()),
                )
                decision_store.record_stakeholder_escalations(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    workflow_id=pm_step.ref.require_workflow_id(),
                    source_operation_id=pm_step.ref.require_operation_id(),
                    source_attempt_id=pm_step.ref.require_attempt_id(),
                    parent_issue_key=parent_detail.key,
                    escalations=tuple(getattr(pm_resolution, "stakeholder_escalations", ()) or ()),
                )
            except Exception as exc:  # noqa: BLE001
                fail_workflow_step_attempt(
                    lifecycle=lifecycle,
                    step=pm_step,
                    category=classify_external_workflow_failure(error=exc),
                    message=str(exc),
                )
                fail_workflow_step_attempt(
                    lifecycle=lifecycle,
                    step=planning_step,
                    category=classify_external_workflow_failure(error=exc),
                    message=str(exc),
                )
                raise

            stakeholder_questions = _stakeholder_escalation_questions(pm_resolution)
            if stakeholder_questions:
                complete_workflow_step_attempt(
                    lifecycle=lifecycle,
                    step=pm_step,
                    summary="PM escalated stakeholder-owned product clarification.",
                )
                self._publish_clarification_then_wait_step(
                    lifecycle=lifecycle,
                    blocking_step=planning_step,
                    parent_detail=parent_detail,
                    questions=stakeholder_questions,
                    context="PM decision resolution",
                )
                return self._deps.fanout_service.blocked_planning_result(
                    planning_result=planning_result,
                    planning_package=planning_package,
                    questions=stakeholder_questions,
                )

            complete_workflow_step_attempt(
                lifecycle=lifecycle,
                step=pm_step,
                summary="PM resolved specialist product decision requests internally.",
            )
            product_brief = _augment_product_brief_with_pm_resolution(
                product_brief=product_brief,
                pm_resolution=pm_resolution,
            )
            planner = self._deps.brief_planner.with_attempt(attempt_ref=planning_step.ref)
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
            for stage in getattr(planning_result, "stages", ()) or ():
                decision_store.record_technical_decisions(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    workflow_id=planning_step.ref.require_workflow_id(),
                    source_operation_id=planning_step.ref.require_operation_id(),
                    source_attempt_id=planning_step.ref.require_attempt_id(),
                    parent_issue_key=parent_detail.key,
                    source_stage=str(getattr(stage, "planning_state", "") or "").strip() or None,
                    decisions=tuple(getattr(stage, "technical_decisions", ()) or ()),
                )
                decision_store.record_pm_requests(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    workflow_id=planning_step.ref.require_workflow_id(),
                    source_operation_id=planning_step.ref.require_operation_id(),
                    source_attempt_id=planning_step.ref.require_attempt_id(),
                    parent_issue_key=parent_detail.key,
                    source_stage=str(getattr(stage, "planning_state", "") or "").strip() or None,
                    requests=tuple(getattr(stage, "pm_decision_requests", ()) or ()),
                )
            if getattr(planning_result, "pm_decision_requests", ()) or ():
                fail_workflow_step_attempt(
                    lifecycle=lifecycle,
                    step=planning_step,
                    category="invalid_model_output",
                    message="Specialist planning returned PM decision requests after PM decision resolution.",
                )
                raise RuntimeError("Specialist planning returned PM decision requests after PM decision resolution.")

        if planning_result.planning_state == PLANNING_STATE_COMPLETED:
            complete_workflow_step_attempt(
                lifecycle=lifecycle,
                step=planning_step,
                summary=planning_summary,
            )
        else:
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=planning_step,
                category="invalid_model_output",
                message="Specialist planning blocked without PM decision requests.",
            )
            raise RuntimeError("Specialist planning blocked without PM decision requests.")
        fanout_step = start_workflow_step_attempt(lifecycle=lifecycle, operation_type=PARENT_OP_JIRA_CHILD_FANOUT)
        fanout = execute_child_fanout_step(
            request=ChildFanoutExecutionInput(
                lifecycle=lifecycle,
                step=fanout_step,
                child_sync_gateway=self._deps.child_sync_gateway,
                fanout_service=self._deps.fanout_service,
                parent_detail=parent_detail,
                project_key=project_key,
                planning_result=planning_result,
                planning_package=planning_package,
                completion_summary=fanout_summary,
            )
        )
        if fanout.completed:
            target_status = _promotion_target_for_parent_status(parent_detail.status)
            promotion_step = start_workflow_step_attempt(
                lifecycle=lifecycle,
                operation_type=PARENT_OP_JIRA_CHILD_PROMOTION,
            )
            try:
                if target_status is None:
                    complete_workflow_step_attempt(
                        lifecycle=lifecycle,
                        step=promotion_step,
                        summary=(
                            f"Child promotion not required while parent issue {parent_detail.key} "
                            f"is {parent_detail.status or 'not on the working board'}."
                        ),
                    )
                else:
                    promotion_result = self._promote_child_issues_work_unit(
                        lifecycle=lifecycle,
                        step=promotion_step,
                        issue_gateway=self._deps.issue_gateway,
                        parent_issue_key=parent_detail.key,
                        target_status=target_status,
                        idempotency_context="post_fanout",
                    )
                    complete_workflow_step_attempt(
                        lifecycle=lifecycle,
                        step=promotion_step,
                        summary=self._promotion_summary(
                            target_status=target_status,
                            promoted_children=list(promotion_result["promoted_children"]),
                        ),
                    )
            except Exception as exc:  # noqa: BLE001
                fail_workflow_step_attempt(
                    lifecycle=lifecycle,
                    step=promotion_step,
                    category=classify_external_workflow_failure(error=exc),
                    message=str(exc),
                )
                raise
            self._publish_start_development_link_step(
                lifecycle=lifecycle,
                settings=settings,
                parent_detail=parent_detail,
                tenant_id=tenant_id,
                project_id=project_id,
            )
            lifecycle.mark_completed_if_ready()
        return fanout

    def _publish_start_development_link_step(
        self,
        *,
        lifecycle,
        settings,  # noqa: ANN001
        parent_detail,
        tenant_id: str,
        project_id: str | None,
    ) -> None:
        if self._operation_completed(
            lifecycle=lifecycle,
            operation_type=PARENT_OP_DEVELOPMENT_START_LINK_PROJECTION,
        ):
            return
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            raise RuntimeError(f"Project is required to publish the start development link for {parent_detail.key}")
        action_url = build_start_work_action_url(
            admin_ui_base_url=settings.admin_ui_base_url,
            claims=StartWorkActionTokenClaims(
                tenant_id=tenant_id,
                project_id=normalized_project_id,
                execution_id=lifecycle.workflow.execution_id,
                workflow_id=lifecycle.workflow.workflow_id,
                issue_key=parent_detail.key,
            ),
            secret=settings.jira_action_token_secret,
        )
        step = start_workflow_step_attempt(
            lifecycle=lifecycle,
            operation_type=PARENT_OP_DEVELOPMENT_START_LINK_PROJECTION,
            target_system="jira",
            target_ref=parent_detail.key,
            summary="Publish Jira action link for starting ready development work.",
        )
        try:
            created_comment, error = self._deps.issue_gateway.publish_start_development_link(
                issue_key=parent_detail.key,
                action_url=action_url,
            )
            if error is not None or created_comment is None:
                raise RuntimeError(error or "Jira start development link comment was not created")
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
            summary="Published Jira action link for starting ready development work.",
        )

    def _operation_completed(self, *, lifecycle, operation_type: str) -> bool:  # noqa: ANN001
        operation = lifecycle.session.execute(
            select(WorkflowOperation).where(
                WorkflowOperation.workflow_id == lifecycle.workflow.workflow_id,
                WorkflowOperation.operation_type == operation_type,
            )
        ).scalar_one_or_none()
        return str(getattr(operation, "status", "") or "").strip().lower() == "completed"

    def _publish_clarification_then_wait_step(
        self,
        *,
        lifecycle,
        blocking_step,
        parent_detail,
        questions: tuple[ClarificationQuestion, ...],
        context: str,
    ) -> None:
        self._clarification_steps().publish_then_wait(
            lifecycle=lifecycle,
            blocking_step=blocking_step,
            parent_detail=parent_detail,
            questions=questions,
            context=context,
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
        supports=(PARENT_OP_BACKLOG_PLANNING, PARENT_OP_PM_DECISION_RESOLUTION, PARENT_OP_JIRA_CHILD_FANOUT),
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
        if publisher is not self._deps.issue_gateway:
            raise RuntimeError("Parent planning clarification projection must use the configured issue gateway")
        return self._clarification_steps().ensure_projection_attempts(
            lifecycle=lifecycle,
            issue_key=issue_key,
            questions=questions,
        )

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
