from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.architecture_document_service import ArchitectureDocumentService
from orchestrator.core.clarification_projection_service import (
    ClarificationProjectionSpec,
    has_matching_active_clarification_state,
    upsert_clarification_projection,
)
from orchestrator.core.followup_context_service import FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION
from orchestrator.core.jira_links import (
    architecture_document_remote_link_spec,
    workflow_execution_remote_link_spec,
)
from orchestrator.core.jira_parent_child_sync_publishers import (
    post_engineering_clarification_questions_to_jira as _post_engineering_clarification_questions_to_jira,
    upsert_jira_remote_link as _upsert_jira_remote_link,
    update_issue_sync_label as _update_issue_sync_label,
)
from orchestrator.core.jira_parent_child_sync_shared import JiraParentChildSyncContext
from orchestrator.core.parent_feature_brief_store import resolve_parent_feature_brief
from orchestrator.core.parent_feature_workflow.adapters import _ParentBriefPlanner, _ParentChildSyncGateway
from orchestrator.core.parent_feature_workflow.dependencies import ParentFeatureWorkflowHandlerDeps
from orchestrator.core.parent_feature_workflow.operations import (
    PARENT_OP_BACKLOG_PLANNING,
    PARENT_OP_JIRA_CHILD_FANOUT,
    PARENT_OP_JIRA_COMMENT_PROJECTION,
    PARENT_OP_JIRA_PARENT_UPDATE,
    PARENT_PROJECTION_OPERATION_TYPES,
    PARENT_RETRYABLE_OPERATION_TYPES,
)
from orchestrator.core.parent_planning_clarification_service import (
    ClarificationPublishEffects,
    ParentPlanningClarificationService,
)
from orchestrator.core.parent_planning_fanout_service import ParentPlanningFanoutResult, ParentPlanningFanoutSeedError, ParentPlanningFanoutService
from orchestrator.core.specialist_planning import PLANNING_STATE_COMPLETED
from orchestrator.core.workflow_advance import (
    InvalidWorkflowOperationRetryError,
    UnsupportedWorkflowOperationRetryError,
)
from orchestrator.core.workflow_execution_projection import (
    WorkflowExecutionProjection,
    classify_external_workflow_failure,
)
from orchestrator.core.workflow_operation_service import WorkflowOperationHandle
from orchestrator.storage.models import Project, Tenant, WorkflowExecution, WorkflowOperation


def _validate_parent_workflow_operation_retry(*, operation_type: str) -> None:
    normalized = str(operation_type or "").strip()
    if normalized in PARENT_RETRYABLE_OPERATION_TYPES:
        return
    if normalized in PARENT_PROJECTION_OPERATION_TYPES:
        raise UnsupportedWorkflowOperationRetryError(
            f"Operation {normalized} is a projection step and must be retried by rerunning its owning workflow operation."
        )
    raise UnsupportedWorkflowOperationRetryError(
        f"Parent feature workflow does not support retry for operation {normalized or '<missing>'}."
    )


def _jira_issue_key_for_workflow(workflow: WorkflowExecution) -> str:
    if str(workflow.source_system or "").strip() != "jira":
        raise InvalidWorkflowOperationRetryError("Parent feature retry requires a Jira workflow source")
    issue_key = str(workflow.source_ref or "").strip().upper()
    if not issue_key:
        raise InvalidWorkflowOperationRetryError("Parent feature retry requires a Jira issue source reference")
    return issue_key


@dataclass(frozen=True)
class _ParentWorkflowRetryContext:
    session: Session
    settings: Any
    workflow: WorkflowExecution
    operation: WorkflowOperation
    tenant: Tenant
    project: Project


class ParentFeatureWorkflowOperationRetryHandler:
    def __init__(
        self,
        *,
        deps: ParentFeatureWorkflowHandlerDeps,
    ) -> None:
        self._deps = deps

    def retry_operation(
        self,
        *,
        session: Session,
        settings,  # noqa: ANN001
        session_factory,
        workflow_type,
        workflow: WorkflowExecution,
        operation: WorkflowOperation,
    ) -> WorkflowOperationHandle:
        _ = session_factory, workflow_type
        _validate_parent_workflow_operation_retry(operation_type=str(operation.operation_type or "").strip())
        tenant = session.get(Tenant, workflow.tenant_id)
        if tenant is None:
            raise InvalidWorkflowOperationRetryError(f"Tenant {workflow.tenant_id} was not found")
        if not workflow.project_id:
            raise InvalidWorkflowOperationRetryError("Workflow is not bound to a project")
        project = session.get(Project, workflow.project_id)
        if project is None:
            raise InvalidWorkflowOperationRetryError(f"Project {workflow.project_id} was not found")
        if not str(project.jira_project_key or "").strip():
            raise InvalidWorkflowOperationRetryError("Project Jira key is required for parent workflow operations")
        context = _ParentWorkflowRetryContext(
            session=session,
            settings=settings,
            workflow=workflow,
            operation=operation,
            tenant=tenant,
            project=project,
        )
        if operation.operation_type == PARENT_OP_JIRA_PARENT_UPDATE:
            return self._retry_jira_parent_update(context=context)
        if operation.operation_type == PARENT_OP_JIRA_CHILD_FANOUT:
            return self._retry_jira_child_fanout(context=context)
        raise UnsupportedWorkflowOperationRetryError(
            f"Parent feature workflow does not support retry for operation {operation.operation_type}"
        )

    def _operation_handle(self, *, context: _ParentWorkflowRetryContext, operation: WorkflowOperation) -> WorkflowOperationHandle:
        return WorkflowOperationHandle(
            operation_id=operation.operation_id,
            workflow_id=context.workflow.workflow_id,
            operation_type=operation.operation_type,
            status=operation.status,
        )

    def _retry_jira_parent_update(self, *, context: _ParentWorkflowRetryContext) -> WorkflowOperationHandle:
        parent_issue_key = _jira_issue_key_for_workflow(context.workflow)
        jira_adapter = self._deps.integration_router.jira(
            session=context.session,
            tenant=context.tenant,
            settings=context.settings,
        )
        oauth = jira_adapter.oauth_context
        parent_detail = jira_adapter.get_issue_detail(issue_id_or_key=parent_issue_key)
        architecture_gate = ArchitectureDocumentService(settings_factory=lambda: context.settings).resolve_gate(
            session=context.session,
            project=context.project,
            parent_issue_key=parent_issue_key,
            issue_summary=parent_detail.summary,
            issue_labels=list(parent_detail.labels or []),
            actor="system",
        )
        if architecture_gate.required and architecture_gate.document is None:
            raise InvalidWorkflowOperationRetryError(
                architecture_gate.block_reason or f"Architecture document link is required for {parent_issue_key}"
            )
        architecture_document = architecture_gate.document
        lifecycle = WorkflowExecutionProjection(session=context.session, workflow=context.workflow)
        operation, attempt = lifecycle.start_operation_attempt(operation_type=context.operation.operation_type)
        try:
            _update_issue_sync_label(
                oauth=oauth,
                parent_detail=parent_detail,
                target_label="children_syncing",
            )
            _upsert_jira_remote_link(
                oauth=oauth,
                issue_key=parent_issue_key,
                spec=workflow_execution_remote_link_spec(
                    admin_ui_base_url=context.settings.admin_ui_base_url,
                    workflow=context.workflow,
                ),
            )
            if architecture_document is not None:
                title = str(architecture_document.title or "").strip()
                url = str(architecture_document.canonical_url or "").strip()
                if not title or not url:
                    raise InvalidWorkflowOperationRetryError(
                        f"Architecture document link is incomplete for {parent_issue_key}"
                    )
                _upsert_jira_remote_link(
                    oauth=oauth,
                    issue_key=parent_issue_key,
                    spec=architecture_document_remote_link_spec(
                        issue_key=parent_issue_key,
                        title=title,
                        url=url,
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            lifecycle.fail_started_operation(
                operation=operation,
                attempt=attempt,
                category=classify_external_workflow_failure(error=exc),
                message=str(exc),
            )
            return self._operation_handle(context=context, operation=operation)
        lifecycle.complete_started_operation(
            operation=operation,
            attempt=attempt,
            summary="Parent Jira metadata and reference links synced without modifying the description.",
        )
        return self._operation_handle(context=context, operation=operation)

    def _retry_jira_child_fanout(self, *, context: _ParentWorkflowRetryContext) -> WorkflowOperationHandle:
        parent_issue_key = _jira_issue_key_for_workflow(context.workflow)
        lifecycle = WorkflowExecutionProjection(session=context.session, workflow=context.workflow)
        operation, attempt = lifecycle.start_operation_attempt(operation_type=context.operation.operation_type)
        brief = resolve_parent_feature_brief(
            session=context.session,
            tenant_id=context.tenant.tenant_id,
            parent_issue_key=parent_issue_key,
        )
        if brief is None:
            lifecycle.fail_started_operation(
                operation=operation,
                attempt=attempt,
                category="missing_input",
                message=f"No confirmed parent brief snapshot is available for {parent_issue_key}",
            )
            return self._operation_handle(context=context, operation=operation)
        jira_adapter = self._deps.integration_router.jira(
            session=context.session,
            tenant=context.tenant,
            settings=context.settings,
        )
        parent_detail = jira_adapter.get_issue_detail(issue_id_or_key=parent_issue_key)
        planning_operation, planning_attempt = lifecycle.start_operation_attempt(operation_type=PARENT_OP_BACKLOG_PLANNING)
        planning_context = JiraParentChildSyncContext(
            request_id=f"workflow-operation:{planning_operation.operation_id}",
            tenant_id=context.tenant.tenant_id,
            tenant=context.tenant,
            project_id=context.project.project_id,
            workflow_id=context.workflow.workflow_id,
            operation_id=planning_operation.operation_id,
            attempt=planning_attempt.attempt_number,
            attempt_id=planning_attempt.attempt_id,
            issue_key=parent_issue_key,
            issue_labels=list(parent_detail.labels or []),
            payload={},
            webhook_event="admin_operation_retry",
            comment_command=None,
            comment_command_argument=None,
        )
        planner = _ParentBriefPlanner(
            session=context.session,
            settings=context.settings,
            context=planning_context,
            build_runtime_for_selector_fn=self._deps.build_runtime_for_selector_fn,
        )
        fanout_service = ParentPlanningFanoutService()
        try:
            planning_result, planning_package = planner.plan_backlog_parent(
                parent_detail=parent_detail,
                product_brief=brief.to_payload(),
                project_key=context.project.jira_project_key,
            )
            if planning_result.planning_state == PLANNING_STATE_COMPLETED:
                lifecycle.complete_started_operation(
                    operation=planning_operation,
                    attempt=planning_attempt,
                    summary="Backlog planning completed from the confirmed parent brief.",
                )
            else:
                lifecycle.wait_started_operation(
                    operation=planning_operation,
                    attempt=planning_attempt,
                    summary="Backlog planning is waiting for product clarification.",
                )
        except ParentPlanningFanoutSeedError as exc:
            lifecycle.fail_started_operation(
                operation=planning_operation,
                attempt=planning_attempt,
                category=classify_external_workflow_failure(error=exc.error),
                message=str(exc.error),
            )
            return self._operation_handle(context=context, operation=planning_operation)
        except Exception as exc:  # noqa: BLE001
            lifecycle.fail_started_operation(
                operation=planning_operation,
                attempt=planning_attempt,
                category=classify_external_workflow_failure(error=exc),
                message=str(exc),
            )
            return self._operation_handle(context=context, operation=planning_operation)

        fanout_context = JiraParentChildSyncContext(
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
        child_sync_gateway = _ParentChildSyncGateway(
            session=context.session,
            context=fanout_context,
            seed_issues_with_runtime_fn=self._deps.seed_issues_with_runtime_fn,
        )
        try:
            seed_data = child_sync_gateway.seed_parent_backlog_children(
                parent_detail=parent_detail,
                project_key=context.project.jira_project_key,
                planning_package=planning_package,
                planning_state=planning_result.planning_state,
            )
            seed_evaluation = fanout_service.evaluate_seed_data(
                seed_data=seed_data,
                combine_child_updates_fn=child_sync_gateway.combined_child_updates,
                planning_result=planning_result,
            )
            fanout = ParentPlanningFanoutResult(
                planning_result=planning_result,
                planning_package=planning_package,
                seed_evaluation=seed_evaluation,
            )
        except ParentPlanningFanoutSeedError as exc:
            lifecycle.fail_started_operation(
                operation=operation,
                attempt=attempt,
                category=classify_external_workflow_failure(error=exc.error),
                message=str(exc.error),
            )
            return self._operation_handle(context=context, operation=operation)
        except Exception as exc:  # noqa: BLE001
            lifecycle.fail_started_operation(
                operation=operation,
                attempt=attempt,
                category=classify_external_workflow_failure(error=exc),
                message=str(exc),
            )
            return self._operation_handle(context=context, operation=operation)
        if not fanout.completed:
            questions = fanout.questions
            clarification_service = ParentPlanningClarificationService()
            clarification_service.ensure_active_clarification(
                issue_key=parent_issue_key,
                questions=questions,
                publisher=_ParentWorkflowEngineeringClarificationPublisher(
                    session=context.session,
                    settings=context.settings,
                    tenant=context.tenant,
                    project=context.project,
                    workflow=context.workflow,
                    create_jira_comment_fn=self._deps.create_jira_comment_fn,
                ),
            )
            message = clarification_service.build_missing_input_message(
                issue_key=parent_issue_key,
                questions=questions,
            )
            lifecycle.fail_started_operation(
                operation=operation,
                attempt=attempt,
                category="missing_input",
                message=message,
            )
            return self._operation_handle(context=context, operation=operation)
        lifecycle.complete_started_operation(
            operation=operation,
            attempt=attempt,
            summary="Engineering child fanout completed from the confirmed parent brief.",
        )
        return self._operation_handle(context=context, operation=operation)


class _ParentWorkflowEngineeringClarificationPublisher:
    def __init__(
        self,
        *,
        session: Session,
        settings,  # noqa: ANN001
        tenant: Tenant,
        project: Project,
        workflow: WorkflowExecution,
        create_jira_comment_fn,
    ) -> None:
        self._session = session
        self._settings = settings
        self._tenant = tenant
        self._project = project
        self._workflow = workflow
        self._create_jira_comment_fn = create_jira_comment_fn

    def has_active_clarification(
        self,
        *,
        issue_key: str,
        questions: tuple,
    ) -> bool:
        return has_matching_active_clarification_state(
            session=self._session,
            tenant_id=self._tenant.tenant_id,
            issue_key=issue_key,
            context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
            questions=questions,
        )

    def publish_clarification(
        self,
        *,
        issue_key: str,
        questions: tuple,
    ) -> ClarificationPublishEffects:
        projection = upsert_clarification_projection(
            session=self._session,
            spec=ClarificationProjectionSpec(
                tenant_id=self._tenant.tenant_id,
                project_id=self._project.project_id,
                context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
                issue_key=issue_key,
                request_id=f"engineering-clarification:{issue_key}",
                origin_command="clarify",
                questions=questions,
                metadata={
                    "questions": [question.to_payload() for question in questions],
                    "source": "workflow_operation_retry",
                },
            ),
        )
        created_comment = None
        error = None
        if not bool(getattr(projection, "already_projected", False)):
            lifecycle = WorkflowExecutionProjection(session=self._session, workflow=self._workflow)
            operation, attempt = lifecycle.start_operation_attempt(operation_type=PARENT_OP_JIRA_COMMENT_PROJECTION)
            try:
                created_comment, error = _post_engineering_clarification_questions_to_jira(
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
                lifecycle.complete_started_operation(
                    operation=operation,
                    attempt=attempt,
                    summary="Posted engineering clarification questions to Jira.",
                )
            else:
                message = error or f"Jira engineering clarification projection did not create a comment for {issue_key}"
                lifecycle.fail_started_operation(
                    operation=operation,
                    attempt=attempt,
                    category="external_failure",
                    message=message,
                )
                raise RuntimeError(message)
        return ClarificationPublishEffects(
            state_recorded=True,
            jira_comment_created=error is None and created_comment is not None,
            discord_followup_created=False,
        )
