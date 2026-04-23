from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.clarification_questions import ClarificationQuestion, ClarificationQuestionSet
from orchestrator.api.webhooks.contracts import create_jira_comment
from orchestrator.core.architecture_document_service import ArchitectureDocumentService
from orchestrator.core.clarification_projection_service import (
    ClarificationProjectionSpec,
    has_matching_active_clarification_state,
    upsert_clarification_projection,
)
from orchestrator.core.config import Settings
from orchestrator.core.followup_context_service import FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION
from orchestrator.core.workflow_execution_status import (
    mark_workflow_failed,
    mark_workflow_running,
    recompute_workflow_status,
)
from orchestrator.core.jira_parent_child_sync_service import (
    PLANNING_STATE_COMPLETED,
    _ParentBriefPlanner,
    _ParentChildSyncGateway,
    JiraParentChildSyncContext,
)
from orchestrator.core.jira_parent_child_sync_publishers import (
    post_engineering_clarification_questions_to_jira as _post_engineering_clarification_questions_to_jira,
    update_issue_sync_label as _update_issue_sync_label,
    upsert_jira_remote_link as _upsert_jira_remote_link,
)
from orchestrator.core.jira_links import (
    architecture_document_remote_link_spec,
    workflow_execution_remote_link_spec,
)
from orchestrator.core.parent_planning_clarification_service import (
    ClarificationPublishEffects,
    ParentPlanningClarificationService,
)
from orchestrator.core.parent_planning_fanout_service import ParentPlanningFanoutSeedError, ParentPlanningFanoutService
from orchestrator.core.parent_feature_brief_store import resolve_parent_feature_brief
from orchestrator.core.workflow_operation_service import (
    WorkflowOperationHandle,
    complete_workflow_operation,
    fail_workflow_operation,
    start_workflow_operation_attempt,
    upsert_workflow_operation,
)
from orchestrator.core.workflow_type_catalog import list_workflow_type_operations
from orchestrator.storage.models import Project, Tenant, WorkflowExecution, WorkflowOperation

logger = logging.getLogger(__name__)


class WorkflowOperationExecutionError(RuntimeError):
    """Base error for workflow operation execution."""


class UnsupportedWorkflowOperationError(WorkflowOperationExecutionError):
    """Raised when no executor exists for the selected operation type."""


class InvalidWorkflowOperationError(WorkflowOperationExecutionError):
    """Raised when workflow-operation data is incomplete or invalid."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _OperationExecutionContext:
    session: Session
    settings: Settings
    session_factory: sessionmaker[Session]
    workflow: WorkflowExecution
    operation: WorkflowOperation
    tenant: Tenant
    project: Project

def _classify_operation_failure(*, error: Exception) -> str:
    message = str(error or "").strip()
    lowered = message.lower()
    if "CONTENT_LIMIT_EXCEEDED" in message:
        return "content_limit"
    if "429" in message or "rate limit" in lowered:
        return "rate_limited"
    if "502" in message or "503" in message or "504" in message or "timed out" in lowered:
        return "transient_external_failure"
    return "external_failure"


def _workflow_operation_retry_executor_reasons() -> dict[str, str]:
    return {
        "jira_comment_projection": "Retry requires the original outbound Jira comment payload, which is not yet persisted.",
        "discord_followup_projection": "Retry requires the original outbound Discord payload, which is not yet persisted.",
        "notification_emit": "Retry requires the original notification projection payload, which is not yet persisted.",
    }


def workflow_operation_retry_unavailable_reason(*, operation_type: str) -> str | None:
    normalized = str(operation_type or "").strip()
    if not normalized:
        return "Workflow operation type is missing."
    if normalized in _workflow_operation_retry_executor_reasons():
        return _workflow_operation_retry_executor_reasons()[normalized]
    return None


def supports_workflow_operation_retry(*, operation_type: str) -> bool:
    return workflow_operation_retry_unavailable_reason(operation_type=operation_type) is None


def _execute_jira_parent_update(
    *,
    context: _OperationExecutionContext,
    integration_router,
    build_runtime_for_selector_fn,
    seed_issues_with_runtime_fn,
) -> WorkflowOperationHandle:
    _ = build_runtime_for_selector_fn, seed_issues_with_runtime_fn
    jira_adapter = integration_router.jira(
        session=context.session,
        tenant=context.tenant,
        settings=context.settings,
    )
    oauth = jira_adapter.oauth_context
    parent_detail = jira_adapter.get_issue_detail(issue_id_or_key=context.workflow.issue_key)
    architecture_gate = ArchitectureDocumentService(settings_factory=lambda: context.settings).resolve_gate(
        session=context.session,
        project=context.project,
        parent_issue_key=context.workflow.issue_key,
        issue_summary=parent_detail.summary,
        issue_labels=list(parent_detail.labels or []),
        actor="system",
    )
    if architecture_gate.required and architecture_gate.document is None:
        raise InvalidWorkflowOperationError(
            architecture_gate.block_reason or f"Architecture document link is required for {context.workflow.issue_key}"
        )
    architecture_document = architecture_gate.document

    attempt = start_workflow_operation_attempt(context.session, operation=context.operation)
    mark_workflow_running(workflow=context.workflow, now=_now())

    try:
        _update_issue_sync_label(
            oauth=oauth,
            parent_detail=parent_detail,
            target_label="children_syncing",
        )
        _upsert_jira_remote_link(
            oauth=oauth,
            issue_key=context.workflow.issue_key,
            spec=workflow_execution_remote_link_spec(
                admin_ui_base_url=context.settings.admin_ui_base_url,
                workflow=context.workflow,
            ),
        )
        if architecture_document is not None:
            title = str(architecture_document.title or "").strip()
            url = str(architecture_document.canonical_url or "").strip()
            if not title or not url:
                raise InvalidWorkflowOperationError(
                    f"Architecture document link is incomplete for {context.workflow.issue_key}"
                )
            _upsert_jira_remote_link(
                oauth=oauth,
                issue_key=context.workflow.issue_key,
                spec=architecture_document_remote_link_spec(
                    issue_key=context.workflow.issue_key,
                    title=title,
                    url=url,
                ),
            )
    except Exception as exc:  # noqa: BLE001
        category = _classify_operation_failure(error=exc)
        logger.exception(
            "workflow_operation_execution_failed workflow_id=%s operation_id=%s operation_type=%s error=%s",
            context.workflow.workflow_id,
            context.operation.operation_id,
            context.operation.operation_type,
            exc,
        )
        fail_workflow_operation(
            context.session,
            operation=context.operation,
            attempt=attempt,
            category=category,
            message=str(exc),
        )
        mark_workflow_failed(workflow=context.workflow, message=str(exc), now=_now())
        return WorkflowOperationHandle(
            operation_id=context.operation.operation_id,
            workflow_id=context.workflow.workflow_id,
            operation_type=context.operation.operation_type,
            status=context.operation.status,
        )

    complete_workflow_operation(
        context.session,
        operation=context.operation,
        attempt=attempt,
        summary="Parent Jira metadata and reference links synced without modifying the description.",
    )
    recompute_workflow_status(session=context.session, workflow=context.workflow, now=_now())
    return WorkflowOperationHandle(
        operation_id=context.operation.operation_id,
        workflow_id=context.workflow.workflow_id,
        operation_type=context.operation.operation_type,
        status=context.operation.status,
    )


def _execute_jira_child_fanout(
    *,
    context: _OperationExecutionContext,
    integration_router,
    build_runtime_for_selector_fn,
    seed_issues_with_runtime_fn,
) -> WorkflowOperationHandle:
    brief = resolve_parent_feature_brief(
        session=context.session,
        tenant_id=context.tenant.tenant_id,
        parent_issue_key=context.workflow.issue_key,
    )
    if brief is None:
        raise InvalidWorkflowOperationError(
            f"No confirmed parent brief snapshot is available for {context.workflow.issue_key}"
        )

    jira_adapter = integration_router.jira(
        session=context.session,
        tenant=context.tenant,
        settings=context.settings,
    )
    attempt = start_workflow_operation_attempt(context.session, operation=context.operation)
    parent_detail = jira_adapter.get_issue_detail(issue_id_or_key=context.workflow.issue_key)
    sync_context = JiraParentChildSyncContext(
        request_id=f"workflow-operation:{context.operation.operation_id}",
        tenant_id=context.tenant.tenant_id,
        tenant=context.tenant,
        project_id=context.project.project_id,
        workflow_id=context.workflow.workflow_id,
        operation_id=context.operation.operation_id,
        attempt=attempt.attempt_number,
        attempt_id=attempt.attempt_id,
        issue_key=context.workflow.issue_key,
        issue_labels=list(parent_detail.labels or []),
        payload={},
        webhook_event="admin_operation_retry",
        comment_command=None,
        comment_command_argument=None,
    )
    planner = _ParentBriefPlanner(
        session=context.session,
        settings=context.settings,
        context=sync_context,
        build_runtime_for_selector_fn=build_runtime_for_selector_fn,
    )
    child_sync_gateway = _ParentChildSyncGateway(
        session=context.session,
        context=sync_context,
        seed_issues_with_runtime_fn=seed_issues_with_runtime_fn,
    )
    clarification_service = ParentPlanningClarificationService()
    fanout_service = ParentPlanningFanoutService()

    mark_workflow_running(workflow=context.workflow, now=_now())

    backlog_planning_operation = upsert_workflow_operation(
        context.session,
        workflow_id=context.workflow.workflow_id,
        operation_type="backlog_planning",
        idempotency_key="workflow-definition:backlog_planning",
        target_system=None,
        target_ref=None,
        summary="Backlog planning completed from the confirmed parent brief.",
    )

    try:
        fanout = fanout_service.plan_and_seed(
            parent_detail=parent_detail,
            product_brief=brief.to_payload(),
            project_key=context.project.jira_project_key,
            planner=planner,
            child_sync_gateway=child_sync_gateway,
        )
    except ParentPlanningFanoutSeedError as exc:
        if exc.planning_result.planning_state == PLANNING_STATE_COMPLETED and backlog_planning_operation.status != "completed":
            planning_attempt = start_workflow_operation_attempt(context.session, operation=backlog_planning_operation)
            complete_workflow_operation(
                context.session,
                operation=backlog_planning_operation,
                attempt=planning_attempt,
                summary="Backlog planning completed from the confirmed parent brief.",
            )
        category = _classify_operation_failure(error=exc.error)
        logger.exception(
            "workflow_operation_execution_failed workflow_id=%s operation_id=%s operation_type=%s error=%s",
            context.workflow.workflow_id,
            context.operation.operation_id,
            context.operation.operation_type,
            exc.error,
        )
        fail_workflow_operation(
            context.session,
            operation=context.operation,
            attempt=attempt,
            category=category,
            message=str(exc.error),
        )
        mark_workflow_failed(workflow=context.workflow, message=str(exc.error), now=_now())
        return WorkflowOperationHandle(
            operation_id=context.operation.operation_id,
            workflow_id=context.workflow.workflow_id,
            operation_type=context.operation.operation_type,
            status=context.operation.status,
        )
    except Exception as exc:  # noqa: BLE001
        category = _classify_operation_failure(error=exc)
        logger.exception(
            "workflow_operation_execution_failed workflow_id=%s operation_id=%s operation_type=%s error=%s",
            context.workflow.workflow_id,
            context.operation.operation_id,
            context.operation.operation_type,
            exc,
        )
        fail_workflow_operation(
            context.session,
            operation=context.operation,
            attempt=attempt,
            category=category,
            message=str(exc),
        )
        mark_workflow_failed(workflow=context.workflow, message=str(exc), now=_now())
        return WorkflowOperationHandle(
            operation_id=context.operation.operation_id,
            workflow_id=context.workflow.workflow_id,
            operation_type=context.operation.operation_type,
            status=context.operation.status,
        )
    if fanout.planning_result.planning_state == PLANNING_STATE_COMPLETED and backlog_planning_operation.status != "completed":
        planning_attempt = start_workflow_operation_attempt(context.session, operation=backlog_planning_operation)
        complete_workflow_operation(
            context.session,
            operation=backlog_planning_operation,
            attempt=planning_attempt,
            summary="Backlog planning completed from the confirmed parent brief.",
        )

    if not fanout.completed:
        questions = fanout.questions
        clarification_service.ensure_active_clarification(
            issue_key=context.workflow.issue_key,
            questions=questions,
            publisher=_WorkflowEngineeringClarificationPublisher(
                context=context,
                create_jira_comment_fn=create_jira_comment,
            ),
        )
        message = clarification_service.build_missing_input_message(
            issue_key=context.workflow.issue_key,
            questions=questions,
        )
        fail_workflow_operation(
            context.session,
            operation=context.operation,
            attempt=attempt,
            category="missing_input",
            message=message,
        )
        mark_workflow_failed(workflow=context.workflow, message=message, now=_now())
        return WorkflowOperationHandle(
            operation_id=context.operation.operation_id,
            workflow_id=context.workflow.workflow_id,
            operation_type=context.operation.operation_type,
            status=context.operation.status,
        )

    complete_workflow_operation(
        context.session,
        operation=context.operation,
        attempt=attempt,
        summary="Engineering child fanout completed from the confirmed parent brief.",
    )
    recompute_workflow_status(session=context.session, workflow=context.workflow, now=_now())
    return WorkflowOperationHandle(
        operation_id=context.operation.operation_id,
        workflow_id=context.workflow.workflow_id,
        operation_type=context.operation.operation_type,
        status=context.operation.status,
    )


class _WorkflowEngineeringClarificationPublisher:
    def __init__(
        self,
        *,
        context: _OperationExecutionContext,
        create_jira_comment_fn,
    ) -> None:
        self._context = context
        self._create_jira_comment_fn = create_jira_comment_fn

    def has_active_clarification(
        self,
        *,
        issue_key: str,
        questions: tuple[ClarificationQuestion, ...],
    ) -> bool:
        return has_matching_active_clarification_state(
            session=self._context.session,
            tenant_id=self._context.tenant.tenant_id,
            issue_key=issue_key,
            context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
            questions=questions,
        )

    def publish_clarification(
        self,
        *,
        issue_key: str,
        questions: tuple[ClarificationQuestion, ...],
    ) -> ClarificationPublishEffects:
        projection = upsert_clarification_projection(
            session=self._context.session,
            spec=ClarificationProjectionSpec(
                tenant_id=self._context.tenant.tenant_id,
                project_id=self._context.project.project_id,
                context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
                issue_key=issue_key,
                request_id=f"engineering-clarification:{issue_key}",
                origin_command="clarify",
                questions=questions,
                metadata={
                    "questions": ClarificationQuestionSet(questions=questions).to_payload(),
                    "source": "workflow_operation_retry",
                },
            ),
        )
        created_comment = None
        error = None
        if not bool(getattr(projection, "already_projected", False)):
            created_comment, error = _post_engineering_clarification_questions_to_jira(
                session=self._context.session,
                tenant=self._context.tenant,
                issue_key=issue_key,
                payload={},
                questions=questions,
                settings=self._context.settings,
                create_jira_comment_fn=self._create_jira_comment_fn,
            )
            if error is None and created_comment is not None:
                comment_operation = upsert_workflow_operation(
                    self._context.session,
                    workflow_id=self._context.workflow.workflow_id,
                    operation_type="jira_comment_projection",
                    idempotency_key="workflow-definition:jira_comment_projection",
                    target_system="jira",
                    target_ref=self._context.workflow.issue_key,
                    summary="Posted engineering clarification questions to Jira.",
                )
                comment_attempt = start_workflow_operation_attempt(self._context.session, operation=comment_operation)
                complete_workflow_operation(
                    self._context.session,
                    operation=comment_operation,
                    attempt=comment_attempt,
                    summary="Posted engineering clarification questions to Jira.",
                )
        jira_comment_created = error is None and created_comment is not None
        return ClarificationPublishEffects(
            state_recorded=True,
            jira_comment_created=jira_comment_created,
            discord_followup_created=False,
        )


def execute_workflow_operation_retry(
    *,
    session: Session,
    settings: Settings,
    session_factory: sessionmaker[Session],
    workflow: WorkflowExecution,
    operation: WorkflowOperation,
    integration_router,
    build_runtime_for_selector_fn: Callable[..., Any],
    seed_issues_with_runtime_fn: Callable[..., Any],
) -> WorkflowOperationHandle:
    definitions = list_workflow_type_operations(session, workflow_type_key=workflow.workflow_type_key)
    definition_operation_types = {
        str(definition.operation_type or "").strip()
        for definition in definitions
        if str(definition.operation_type or "").strip()
    }
    if operation.operation_type not in definition_operation_types:
        raise InvalidWorkflowOperationError(
            f"Operation {operation.operation_type} is not defined for workflow type {workflow.workflow_type_key}"
        )

    tenant = session.get(Tenant, workflow.tenant_id)
    if tenant is None:
        raise InvalidWorkflowOperationError(f"Tenant {workflow.tenant_id} was not found")
    if not workflow.project_id:
        raise InvalidWorkflowOperationError("Workflow is not bound to a project")
    project = session.get(Project, workflow.project_id)
    if project is None:
        raise InvalidWorkflowOperationError(f"Project {workflow.project_id} was not found")
    if not str(project.jira_project_key or "").strip():
        raise InvalidWorkflowOperationError("Project Jira key is required for Jira workflow operations")

    executors: dict[str, Callable[..., WorkflowOperationHandle]] = {
        "jira_parent_update": _execute_jira_parent_update,
        "jira_child_fanout": _execute_jira_child_fanout,
    }
    executor = executors.get(operation.operation_type)
    if executor is None:
        unavailable_reason = workflow_operation_retry_unavailable_reason(operation_type=operation.operation_type)
        if unavailable_reason:
            raise UnsupportedWorkflowOperationError(unavailable_reason)
        raise UnsupportedWorkflowOperationError(f"No workflow operation executor is registered for {operation.operation_type}")

    return executor(
        context=_OperationExecutionContext(
            session=session,
            settings=settings,
            session_factory=session_factory,
            workflow=workflow,
            operation=operation,
            tenant=tenant,
            project=project,
        ),
        integration_router=integration_router,
        build_runtime_for_selector_fn=build_runtime_for_selector_fn,
        seed_issues_with_runtime_fn=seed_issues_with_runtime_fn,
    )
