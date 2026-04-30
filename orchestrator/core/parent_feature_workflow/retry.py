from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from orchestrator.core.architecture_document_service import ArchitectureDocumentService
from orchestrator.core.clarification_projection_service import (
    ClarificationProjectionSpec,
    jira_comment_evidence_id,
    matching_active_jira_clarification_evidence_id,
    upsert_clarification_projection,
)
from orchestrator.core.followup_context_service import (
    FOLLOWUP_CONTEXT_PARENT_PLANNING_CLARIFICATION,
    upsert_followup_context,
)
from orchestrator.core.jira_links import (
    architecture_document_remote_link_spec,
    workflow_execution_remote_link_spec,
)
from orchestrator.core.jira_parent_child_sync_publishers import (
    post_engineering_clarification_questions_to_jira as _post_engineering_clarification_questions_to_jira,
    upsert_jira_remote_link as _upsert_jira_remote_link,
    update_issue_sync_label as _update_issue_sync_label,
)
from orchestrator.core.jira_parent_child_sync_shared import JiraParentChildSyncContext, extract_created_comment_id
from orchestrator.core.parent_feature_brief_store import resolve_parent_feature_brief
from orchestrator.core.parent_feature_workflow.adapters import _ParentBriefPlanner, _ParentChildSyncGateway
from orchestrator.core.parent_feature_workflow.dependencies import ParentFeatureWorkflowHandlerDeps
from orchestrator.core.parent_feature_workflow.operations import (
    PARENT_OP_BACKLOG_PLANNING,
    PARENT_OP_JIRA_CHILD_FANOUT,
    PARENT_OP_JIRA_COMMENT_PROJECTION,
    PARENT_OP_JIRA_PARENT_UPDATE,
)
from orchestrator.core.clarification_questions import ClarificationQuestionSet
from orchestrator.core.parent_planning_clarification_service import (
    ClarificationPublishEffects,
    ParentPlanningClarificationService,
)
from orchestrator.core.parent_planning_fanout_service import ParentPlanningFanoutResult, ParentPlanningFanoutSeedError, ParentPlanningFanoutService
from orchestrator.core.specialist_planning import PLANNING_STATE_COMPLETED, RetryableSpecialistPlanningContractError
from orchestrator.core.workflow_definition import WorkflowDefinition
from orchestrator.core.workflow_advance import (
    InvalidWorkflowOperationRetryError,
    UnsupportedWorkflowOperationRetryError,
    WorkflowOperationRetryCapability,
    WorkflowOperationRetryRequest,
)
from orchestrator.core.workflow_execution_projection import (
    WorkflowExecutionProjection,
    classify_external_workflow_failure,
)
from orchestrator.core.workflow_operation_service import WorkflowOperationHandle
from orchestrator.storage.models import Project, Tenant, WorkflowExecution, WorkflowOperation


def _validate_parent_workflow_operation_retry(
    *,
    workflow_type: WorkflowDefinition,
    operation_type: str,
) -> None:
    normalized = str(operation_type or "").strip()
    try:
        step = workflow_type.step(normalized)
    except LookupError as exc:
        raise UnsupportedWorkflowOperationRetryError(
            f"Workflow {workflow_type.workflow_type_key} has no retryable operation {normalized or '<missing>'}."
        ) from exc
    if not step.retryable:
        raise UnsupportedWorkflowOperationRetryError(
            f"Workflow operation {normalized} is not retryable in workflow {workflow_type.workflow_type_key}."
        )
    return


def _jira_issue_key_for_workflow(workflow: WorkflowExecution) -> str:
    if str(workflow.source_system or "").strip() != "jira":
        raise InvalidWorkflowOperationRetryError("Parent feature retry requires a Jira workflow source")
    issue_key = str(workflow.source_ref or "").strip().upper()
    if not issue_key:
        raise InvalidWorkflowOperationRetryError("Parent feature retry requires a Jira issue source reference")
    return issue_key


def _extract_required_jira_comment_id(*, created_comment: dict[str, Any] | None, issue_key: str) -> str:
    comment_id = extract_created_comment_id(created_comment)
    if not comment_id:
        raise RuntimeError(f"Jira clarification projection for {issue_key} did not return a comment id")
    return comment_id


@dataclass(frozen=True)
class _ParentWorkflowRetryContext:
    session: Session
    settings: Any
    workflow_type: WorkflowDefinition
    workflow: WorkflowExecution
    operation: WorkflowOperation
    tenant: Tenant
    project: Project


@dataclass(frozen=True)
class _ParentFeatureRetryOperationCapability:
    operation_type: str
    execute: Callable[..., WorkflowOperationHandle]


_PARENT_FEATURE_RETRY_OPERATION_SPECS = (
    (PARENT_OP_JIRA_PARENT_UPDATE, "_retry_jira_parent_update"),
    (PARENT_OP_BACKLOG_PLANNING, "_retry_backlog_planning"),
    (PARENT_OP_JIRA_CHILD_FANOUT, "_retry_jira_child_fanout"),
)


class ParentFeatureWorkflowOperationRetryHandler:
    def __init__(
        self,
        *,
        deps: ParentFeatureWorkflowHandlerDeps,
    ) -> None:
        self._deps = deps
        self._retry_capabilities = {
            capability.operation_type: capability
            for capability in tuple(
                _ParentFeatureRetryOperationCapability(
                    operation_type=operation_type,
                    execute=getattr(self, method_name),
                )
                for operation_type, method_name in _PARENT_FEATURE_RETRY_OPERATION_SPECS
            )
        }

    @classmethod
    def declared_operation_retry_capabilities(
        cls,
        workflow_type: WorkflowDefinition,
    ) -> tuple[WorkflowOperationRetryCapability, ...]:
        capabilities: list[WorkflowOperationRetryCapability] = []
        for operation_type, _method_name in _PARENT_FEATURE_RETRY_OPERATION_SPECS:
            if workflow_type.has_step(operation_type) and workflow_type.step(operation_type).retryable:
                capabilities.append(WorkflowOperationRetryCapability(operation_type=operation_type))
        return tuple(capabilities)

    def operation_retry_capabilities(self, workflow_type) -> tuple[WorkflowOperationRetryCapability, ...]:  # noqa: ANN001
        return self.declared_operation_retry_capabilities(workflow_type)

    def retry_operation(
        self,
        *,
        request: WorkflowOperationRetryRequest,
    ) -> WorkflowOperationHandle:
        _validate_parent_workflow_operation_retry(
            workflow_type=request.workflow_type,
            operation_type=str(request.operation.operation_type or "").strip(),
        )
        tenant = request.session.get(Tenant, request.workflow.tenant_id)
        if tenant is None:
            raise InvalidWorkflowOperationRetryError(f"Tenant {request.workflow.tenant_id} was not found")
        if not request.workflow.project_id:
            raise InvalidWorkflowOperationRetryError("Workflow is not bound to a project")
        project = request.session.get(Project, request.workflow.project_id)
        if project is None:
            raise InvalidWorkflowOperationRetryError(f"Project {request.workflow.project_id} was not found")
        if not str(project.jira_project_key or "").strip():
            raise InvalidWorkflowOperationRetryError("Project Jira key is required for parent workflow operations")
        context = _ParentWorkflowRetryContext(
            session=request.session,
            settings=request.settings,
            workflow_type=request.workflow_type,
            workflow=request.workflow,
            operation=request.operation,
            tenant=tenant,
            project=project,
        )
        capability = self._retry_capability(operation_type=str(request.operation.operation_type or "").strip())
        return capability.execute(context=context)

    def _retry_capability(self, *, operation_type: str) -> _ParentFeatureRetryOperationCapability:
        capability = self._retry_capabilities.get(operation_type)
        if capability is None:
            raise UnsupportedWorkflowOperationRetryError(
                f"Parent feature workflow operation {operation_type} is retryable but has no retry implementation."
            )
        return capability

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
        lifecycle = WorkflowExecutionProjection(
            session=context.session,
            workflow=context.workflow,
            workflow_type=context.workflow_type,
        )
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

    def _retry_backlog_planning(self, *, context: _ParentWorkflowRetryContext) -> WorkflowOperationHandle:
        parent_issue_key = _jira_issue_key_for_workflow(context.workflow)
        lifecycle = WorkflowExecutionProjection(
            session=context.session,
            workflow=context.workflow,
            workflow_type=context.workflow_type,
        )
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
        planning_context = JiraParentChildSyncContext(
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
        planner = _ParentBriefPlanner(
            session=context.session,
            settings=context.settings,
            context=planning_context,
            build_runtime_for_selector_fn=self._deps.build_runtime_for_selector_fn,
        )
        try:
            planning_result, planning_package = planner.plan_backlog_parent(
                parent_detail=parent_detail,
                product_brief=brief.to_payload(),
                project_key=context.project.jira_project_key,
            )
        except RetryableSpecialistPlanningContractError as exc:
            lifecycle.fail_started_operation(
                operation=operation,
                attempt=attempt,
                category="invalid_model_output",
                message=str(exc),
            )
            raise
        except Exception as exc:  # noqa: BLE001
            lifecycle.fail_started_operation(
                operation=operation,
                attempt=attempt,
                category=classify_external_workflow_failure(error=exc),
                message=str(exc),
            )
            return self._operation_handle(context=context, operation=operation)
        if planning_result.planning_state == PLANNING_STATE_COMPLETED:
            lifecycle.complete_started_operation(
                operation=operation,
                attempt=attempt,
                summary="Backlog planning completed from the confirmed parent brief.",
            )
            return self._run_child_fanout_from_planning(
                context=context,
                lifecycle=lifecycle,
                parent_issue_key=parent_issue_key,
                parent_detail=parent_detail,
                planning_result=planning_result,
                planning_package=planning_package,
            )
        else:
            questions = ClarificationQuestionSet.from_values(
                getattr(planning_result, "open_behavior_questions", ()) or ()
            ).questions
            self._wait_for_engineering_clarification(
                context=context,
                lifecycle=lifecycle,
                operation=operation,
                attempt=attempt,
                parent_issue_key=parent_issue_key,
                questions=questions,
                clarification_context="Backlog planning",
            )
        return self._operation_handle(context=context, operation=operation)

    def _run_child_fanout_from_planning(
        self,
        *,
        context: _ParentWorkflowRetryContext,
        lifecycle: WorkflowExecutionProjection,
        parent_issue_key: str,
        parent_detail,
        planning_result,
        planning_package: dict[str, Any],
    ) -> WorkflowOperationHandle:
        operation, attempt = lifecycle.start_operation_attempt(operation_type=PARENT_OP_JIRA_CHILD_FANOUT)
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
        fanout_service = ParentPlanningFanoutService()
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
        except Exception as exc:  # noqa: BLE001
            lifecycle.fail_started_operation(
                operation=operation,
                attempt=attempt,
                category=classify_external_workflow_failure(error=exc),
                message=str(exc),
            )
            return self._operation_handle(context=context, operation=operation)
        if not fanout.completed:
            self._wait_for_engineering_clarification(
                context=context,
                lifecycle=lifecycle,
                operation=operation,
                attempt=attempt,
                parent_issue_key=parent_issue_key,
                questions=fanout.questions,
                clarification_context="Engineering child fanout",
            )
            return self._operation_handle(context=context, operation=operation)
        lifecycle.complete_started_operation(
            operation=operation,
            attempt=attempt,
            summary="Engineering child fanout completed from the confirmed parent brief.",
        )
        return self._operation_handle(context=context, operation=operation)

    def _retry_jira_child_fanout(self, *, context: _ParentWorkflowRetryContext) -> WorkflowOperationHandle:
        parent_issue_key = _jira_issue_key_for_workflow(context.workflow)
        lifecycle = WorkflowExecutionProjection(
            session=context.session,
            workflow=context.workflow,
            workflow_type=context.workflow_type,
        )
        brief = resolve_parent_feature_brief(
            session=context.session,
            tenant_id=context.tenant.tenant_id,
            parent_issue_key=parent_issue_key,
        )
        if brief is None:
            operation, attempt = lifecycle.start_operation_attempt(operation_type=context.operation.operation_type)
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
                questions = ClarificationQuestionSet.from_values(
                    getattr(planning_result, "open_behavior_questions", ()) or ()
                ).questions
                self._wait_for_engineering_clarification(
                    context=context,
                    lifecycle=lifecycle,
                    operation=planning_operation,
                    attempt=planning_attempt,
                    parent_issue_key=parent_issue_key,
                    questions=questions,
                    clarification_context="Backlog planning",
                )
                return self._operation_handle(context=context, operation=planning_operation)
        except RetryableSpecialistPlanningContractError as exc:
            lifecycle.fail_started_operation(
                operation=planning_operation,
                attempt=planning_attempt,
                category="invalid_model_output",
                message=str(exc),
            )
            raise
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

        operation, attempt = lifecycle.start_operation_attempt(operation_type=context.operation.operation_type)
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
            self._wait_for_engineering_clarification(
                context=context,
                lifecycle=lifecycle,
                operation=operation,
                attempt=attempt,
                parent_issue_key=parent_issue_key,
                questions=questions,
                clarification_context="Engineering child fanout",
            )
            return self._operation_handle(context=context, operation=operation)
        lifecycle.complete_started_operation(
            operation=operation,
            attempt=attempt,
            summary="Engineering child fanout completed from the confirmed parent brief.",
        )
        return self._operation_handle(context=context, operation=operation)

    def _wait_for_engineering_clarification(
        self,
        *,
        context: _ParentWorkflowRetryContext,
        lifecycle: WorkflowExecutionProjection,
        operation: WorkflowOperation,
        attempt,
        parent_issue_key: str,
        questions: tuple,
        clarification_context: str,
    ) -> None:
        clarification_service = ParentPlanningClarificationService()
        publisher = _ParentWorkflowPlanningClarificationPublisher(
            session=context.session,
            settings=context.settings,
            tenant=context.tenant,
            project=context.project,
            workflow_type=context.workflow_type,
            workflow=context.workflow,
            blocked_operation_type=str(operation.operation_type or "").strip(),
            create_jira_comment_fn=self._deps.create_jira_comment_fn,
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


class _ParentWorkflowPlanningClarificationPublisher:
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
    ) -> None:
        self._session = session
        self._settings = settings
        self._tenant = tenant
        self._project = project
        self._workflow_type = workflow_type
        self._workflow = workflow
        self._blocked_operation_type = blocked_operation_type
        self._create_jira_comment_fn = create_jira_comment_fn

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
        if not jira_comment_id:
            lifecycle = WorkflowExecutionProjection(
                session=self._session,
                workflow=self._workflow,
                workflow_type=self._workflow_type,
            )
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
