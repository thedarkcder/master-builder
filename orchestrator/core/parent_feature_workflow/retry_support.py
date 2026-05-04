from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.clarification.questions import ClarificationQuestion, ClarificationQuestionSet
from orchestrator.core.clarification.projection_service import (
    ClarificationProjectionSpec,
    jira_comment_evidence_id,
    matching_active_jira_clarification_evidence_id,
    upsert_clarification_projection,
)
from orchestrator.core.pm.followup_context_service import (
    FOLLOWUP_CONTEXT_PARENT_PLANNING_CLARIFICATION,
    upsert_followup_context,
)
from orchestrator.core.integrations.atlassian.parent_child_sync_publishers import (
    post_pm_product_clarification_questions_to_jira,
)
from orchestrator.core.integrations.atlassian.parent_child_sync_shared import JiraParentChildSyncContext, extract_created_comment_id
from orchestrator.core.parent_feature_workflow.adapters import _ParentChildSyncGateway
from orchestrator.core.parent_feature_workflow.dependencies import ParentFeatureWorkflowHandlerDeps
from orchestrator.core.parent_feature_workflow.operations import (
    PARENT_OP_JIRA_CHILD_FANOUT,
    PARENT_OP_JIRA_COMMENT_PROJECTION,
)
from orchestrator.core.planning.decision_records import PlanningDecisionRecordStore
from orchestrator.core.projects.parent_planning_clarification_service import (
    ClarificationPublishEffects,
    ParentPlanningClarificationService,
)
from orchestrator.core.parent_feature_workflow.child_fanout_execution import (
    ChildFanoutExecutionError,
    ChildFanoutExecutionInput,
    execute_child_fanout_step,
)
from orchestrator.core.projects.parent_planning_fanout_service import ParentPlanningFanoutSeedError, ParentPlanningFanoutService
from orchestrator.core.workflow.advance import InvalidWorkflowOperationRetryError
from orchestrator.core.workflow.definition import WorkflowDefinition
from orchestrator.core.workflow.execution_projection import WorkflowExecutionProjection, classify_external_workflow_failure
from orchestrator.core.workflow.operation_service import WorkflowOperationHandle
from orchestrator.core.workflow.step_runner import WorkflowStepAttempt
from orchestrator.storage.models import Project, Tenant, WorkflowExecution, WorkflowOperation


@dataclass(frozen=True)
class ParentWorkflowRetryContext:
    session: Session
    settings: Any
    workflow_type: WorkflowDefinition
    workflow: WorkflowExecution
    operation: WorkflowOperation
    tenant: Tenant
    project: Project


def jira_issue_key_for_workflow(workflow: WorkflowExecution) -> str:
    if str(workflow.source_system or "").strip() != "jira":
        raise InvalidWorkflowOperationRetryError("Parent feature retry requires a Jira workflow source")
    issue_key = str(workflow.source_ref or "").strip().upper()
    if not issue_key:
        raise InvalidWorkflowOperationRetryError("Parent feature retry requires a Jira issue source reference")
    return issue_key


def stakeholder_escalation_questions(pm_resolution) -> tuple[ClarificationQuestion, ...]:  # noqa: ANN001
    questions: list[object] = []
    for escalation in getattr(pm_resolution, "stakeholder_escalations", ()) or ():
        to_question = getattr(escalation, "to_clarification_question", None)
        questions.append(to_question() if callable(to_question) else escalation)
    return ClarificationQuestionSet.from_values(questions).questions


def augment_product_brief_with_pm_resolution(*, product_brief: dict[str, Any], pm_resolution) -> dict[str, Any]:  # noqa: ANN001
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


def record_specialist_decisions_for_retry(
    *,
    context: ParentWorkflowRetryContext,
    operation: WorkflowOperation,
    attempt,
    parent_issue_key: str,
    planning_result,
) -> None:
    decision_store = PlanningDecisionRecordStore(session=context.session)
    for stage in getattr(planning_result, "stages", ()) or ():
        decision_store.record_technical_decisions(
            tenant_id=context.tenant.tenant_id,
            project_id=context.project.project_id,
            workflow_id=context.workflow.workflow_id,
            source_operation_id=operation.operation_id,
            source_attempt_id=attempt.attempt_id,
            parent_issue_key=parent_issue_key,
            source_stage=str(getattr(stage, "planning_state", "") or "").strip() or None,
            decisions=tuple(getattr(stage, "technical_decisions", ()) or ()),
        )
        decision_store.record_pm_requests(
            tenant_id=context.tenant.tenant_id,
            project_id=context.project.project_id,
            workflow_id=context.workflow.workflow_id,
            source_operation_id=operation.operation_id,
            source_attempt_id=attempt.attempt_id,
            parent_issue_key=parent_issue_key,
            source_stage=str(getattr(stage, "planning_state", "") or "").strip() or None,
            requests=tuple(getattr(stage, "pm_decision_requests", ()) or ()),
        )


def operation_handle(*, context: ParentWorkflowRetryContext, operation: WorkflowOperation) -> WorkflowOperationHandle:
    return WorkflowOperationHandle(
        operation_id=operation.operation_id,
        workflow_id=context.workflow.workflow_id,
        operation_type=operation.operation_type,
        status=operation.status,
    )


def child_sync_context(
    *,
    context: ParentWorkflowRetryContext,
    operation: WorkflowOperation,
    attempt,
    parent_issue_key: str,
    parent_detail,
) -> JiraParentChildSyncContext:
    return JiraParentChildSyncContext(
        request_id=f"workflow-operation:{operation.operation_id}",
        tenant_id=context.tenant.tenant_id,
        tenant=context.tenant,
        project_id=context.project.project_id,
        workflow_id=context.workflow.workflow_id,
        operation_id=operation.operation_id,
        attempt=attempt.attempt_number,
        attempt_id=attempt.attempt_id,
        issue_key=parent_issue_key,
        issue_labels=list(parent_detail.labels or []),
        payload={},
        webhook_event="admin_operation_retry",
        comment_command=None,
        comment_command_argument=None,
    )


def execute_child_fanout_on_started_attempt(
    *,
    context: ParentWorkflowRetryContext,
    deps: ParentFeatureWorkflowHandlerDeps,
    lifecycle: WorkflowExecutionProjection,
    operation: WorkflowOperation,
    attempt,
    parent_issue_key: str,
    parent_detail,
    planning_result,
    planning_package: dict[str, Any],
) -> WorkflowOperationHandle:
    child_sync_gateway = _ParentChildSyncGateway(
        session=context.session,
        context=child_sync_context(
            context=context,
            operation=operation,
            attempt=attempt,
            parent_issue_key=parent_issue_key,
            parent_detail=parent_detail,
        ),
        seed_issues_with_runtime_fn=deps.seed_issues_with_runtime_fn,
    )
    fanout_service = ParentPlanningFanoutService()
    try:
        execute_child_fanout_step(
            request=ChildFanoutExecutionInput(
                lifecycle=lifecycle,
                step=WorkflowStepAttempt(operation=operation, attempt=attempt),
                child_sync_gateway=child_sync_gateway,
                fanout_service=fanout_service,
                parent_detail=parent_detail,
                project_key=context.project.jira_project_key,
                planning_result=planning_result,
                planning_package=planning_package,
                completion_summary="Engineering child fanout completed from the confirmed parent brief.",
            )
        )
    except (ChildFanoutExecutionError, ParentPlanningFanoutSeedError):
        return operation_handle(context=context, operation=operation)
    return operation_handle(context=context, operation=operation)


def start_child_fanout_from_planning(
    *,
    context: ParentWorkflowRetryContext,
    deps: ParentFeatureWorkflowHandlerDeps,
    lifecycle: WorkflowExecutionProjection,
    parent_issue_key: str,
    parent_detail,
    planning_result,
    planning_package: dict[str, Any],
) -> WorkflowOperationHandle:
    operation, attempt = lifecycle.start_operation_attempt(operation_type=PARENT_OP_JIRA_CHILD_FANOUT)
    return execute_child_fanout_on_started_attempt(
        context=context,
        deps=deps,
        lifecycle=lifecycle,
        operation=operation,
        attempt=attempt,
        parent_issue_key=parent_issue_key,
        parent_detail=parent_detail,
        planning_result=planning_result,
        planning_package=planning_package,
    )


def wait_for_stakeholder_clarification(
    *,
    context: ParentWorkflowRetryContext,
    deps: ParentFeatureWorkflowHandlerDeps,
    lifecycle: WorkflowExecutionProjection,
    operation: WorkflowOperation,
    attempt,
    parent_issue_key: str,
    questions: tuple,
    clarification_context: str,
) -> None:
    clarification_service = ParentPlanningClarificationService()
    jira_adapter = deps.integration_router.jira(
        session=context.session,
        tenant=context.tenant,
        settings=context.settings,
    )
    publisher = ParentWorkflowPlanningClarificationPublisher(
        session=context.session,
        settings=context.settings,
        tenant=context.tenant,
        project=context.project,
        workflow_type=context.workflow_type,
        workflow=context.workflow,
        blocked_operation_type=str(operation.operation_type or "").strip(),
        create_jira_comment_fn=deps.create_jira_comment_fn,
        jira_adapter=jira_adapter,
    )
    try:
        waiting_state = clarification_service.ensure_waiting_clarification(
            issue_key=parent_issue_key,
            questions=questions,
            publisher=publisher,
            context=clarification_context,
        )
    except Exception as exc:  # noqa: BLE001
        category = "contract_violation" if isinstance(exc, ValueError) else classify_external_workflow_failure(error=exc)
        lifecycle.fail_started_operation(
            operation=operation,
            attempt=attempt,
            category=category,
            message=str(exc),
        )
        return
    lifecycle.wait_started_operation(
        operation=operation,
        attempt=attempt,
        summary=waiting_state.message,
    )


def _extract_required_jira_comment_id(*, created_comment: dict[str, Any] | None, issue_key: str) -> str:
    comment_id = extract_created_comment_id(created_comment)
    if not comment_id:
        raise RuntimeError(f"Jira clarification projection for {issue_key} did not return a comment id")
    return comment_id


def _jira_comment_exists(*, jira_adapter: Any, issue_key: str, comment_id: str) -> bool:
    normalized_comment_id = str(comment_id or "").strip()
    if not normalized_comment_id:
        return False
    comments = jira_adapter.list_issue_comments(issue_id_or_key=issue_key)
    return any(str(getattr(comment, "comment_id", "") or "").strip() == normalized_comment_id for comment in comments)


class ParentWorkflowPlanningClarificationPublisher:
    def __init__(
        self,
        *,
        session: Session,
        settings,  # noqa: ANN001
        tenant: Tenant,
        project: Project,
        workflow_type: WorkflowDefinition,
        workflow: WorkflowExecution,
        blocked_operation_type: str,
        create_jira_comment_fn,
        jira_adapter: Any,
    ) -> None:
        self._session = session
        self._settings = settings
        self._tenant = tenant
        self._project = project
        self._workflow_type = workflow_type
        self._workflow = workflow
        self._blocked_operation_type = blocked_operation_type
        self._create_jira_comment_fn = create_jira_comment_fn
        self._jira_adapter = jira_adapter

    def active_clarification_effects(
        self,
        *,
        issue_key: str,
        questions: tuple,
    ) -> ClarificationPublishEffects | None:
        jira_comment_id = matching_active_jira_clarification_evidence_id(
            session=self._session,
            tenant_id=self._tenant.tenant_id,
            issue_key=issue_key,
            context_type=FOLLOWUP_CONTEXT_PARENT_PLANNING_CLARIFICATION,
            questions=questions,
        )
        if jira_comment_id is None:
            return None
        if not _jira_comment_exists(
            jira_adapter=self._jira_adapter,
            issue_key=issue_key,
            comment_id=jira_comment_id,
        ):
            return None
        return ClarificationPublishEffects(
            state_recorded=True,
            jira_comment_created=False,
            discord_followup_created=False,
            jira_comment_id=jira_comment_id,
        )

    def publish_clarification(
        self,
        *,
        issue_key: str,
        questions: tuple,
    ) -> ClarificationPublishEffects:
        metadata = {
            "questions": [question.to_payload() for question in questions],
            "source": "workflow_operation_retry",
            "blocked_operation_type": self._blocked_operation_type,
            "workflow_id": self._workflow.workflow_id,
        }
        projection = upsert_clarification_projection(
            session=self._session,
            spec=ClarificationProjectionSpec(
                tenant_id=self._tenant.tenant_id,
                project_id=self._project.project_id,
                context_type=FOLLOWUP_CONTEXT_PARENT_PLANNING_CLARIFICATION,
                issue_key=issue_key,
                request_id=f"parent-planning-clarification:{issue_key}",
                origin_command="clarify",
                questions=questions,
                metadata=metadata,
            ),
        )
        created_comment = None
        error = None
        jira_comment_id = jira_comment_evidence_id(projection.followup_context)
        if jira_comment_id and not _jira_comment_exists(
            jira_adapter=self._jira_adapter,
            issue_key=issue_key,
            comment_id=jira_comment_id,
        ):
            jira_comment_id = None
        if not jira_comment_id:
            lifecycle = WorkflowExecutionProjection(
                session=self._session,
                workflow=self._workflow,
                workflow_type=self._workflow_type,
            )
            operation, attempt = lifecycle.start_operation_attempt(operation_type=PARENT_OP_JIRA_COMMENT_PROJECTION)
            try:
                created_comment, error = post_pm_product_clarification_questions_to_jira(
                    session=self._session,
                    tenant=self._tenant,
                    issue_key=issue_key,
                    payload={},
                    questions=questions,
                    settings=self._settings,
                    create_jira_comment_fn=self._create_jira_comment_fn,
                )
            except Exception as exc:  # noqa: BLE001
                lifecycle.fail_started_operation(
                    operation=operation,
                    attempt=attempt,
                    category=classify_external_workflow_failure(error=exc),
                    message=str(exc),
                )
                raise
            if error is None and created_comment is not None:
                try:
                    jira_comment_id = _extract_required_jira_comment_id(created_comment=created_comment, issue_key=issue_key)
                except RuntimeError as exc:
                    lifecycle.fail_started_operation(
                        operation=operation,
                        attempt=attempt,
                        category="contract_violation",
                        message=str(exc),
                    )
                    raise
                next_metadata = dict(projection.metadata)
                next_metadata["jira_comment_id"] = jira_comment_id
                upsert_followup_context(
                    session=self._session,
                    tenant_id=self._tenant.tenant_id,
                    project_id=self._project.project_id,
                    context_type=FOLLOWUP_CONTEXT_PARENT_PLANNING_CLARIFICATION,
                    origin_command="clarify",
                    issue_key=issue_key,
                    request_id=f"parent-planning-clarification:{issue_key}",
                    metadata=next_metadata,
                )
                lifecycle.complete_started_operation(
                    operation=operation,
                    attempt=attempt,
                    summary=f"Posted parent planning clarification questions to Jira comment {jira_comment_id}.",
                )
            else:
                message = error or f"Jira parent planning clarification projection did not create a comment for {issue_key}"
                lifecycle.fail_started_operation(
                    operation=operation,
                    attempt=attempt,
                    category="external_failure",
                    message=message,
                )
                raise RuntimeError(message)
        if not jira_comment_id:
            raise RuntimeError(f"Jira parent planning clarification projection for {issue_key} did not record a comment id")
        return ClarificationPublishEffects(
            state_recorded=True,
            jira_comment_created=error is None and created_comment is not None,
            discord_followup_created=False,
            jira_comment_id=jira_comment_id,
        )
