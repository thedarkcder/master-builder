from __future__ import annotations
import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.architecture_document_service import ArchitectureDocumentService
from orchestrator.core.clarification_questions import ClarificationQuestion
from orchestrator.core.clarification_projection_service import (
    ClarificationProjectionSpec,
    has_matching_active_clarification_state,
    upsert_clarification_projection,
)
from orchestrator.core.runtime_invocation import AgentInvocationContext, WorkflowAttemptRef
from orchestrator.core.specialist_planning import (
    PLANNING_STATE_COMPLETED,
    SpecialistPlanningRequest,
    build_runtime_seed_planning_package,
    run_specialist_planning_fanout,
)
from orchestrator.core.followup_context_service import (
    FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
    FOLLOWUP_CONTEXT_PM_INTERVIEW,
)
from orchestrator.core.parent_feature_brief_store import (
    persist_parent_feature_brief_snapshot,
    resolve_parent_feature_brief,
)
from orchestrator.core.jira_links import (
    architecture_document_remote_link_spec,
    workflow_execution_remote_link_spec,
)
from orchestrator.core.jira_parent_child_sync_publishers import (
    mark_issues_sync_blocked as _mark_issues_sync_blocked,
    post_engineering_clarification_questions_to_jira as _post_engineering_clarification_questions_to_jira,
    post_parent_brief_questions_to_discord as _post_parent_brief_questions_to_discord,
    post_parent_brief_questions_to_jira as _post_parent_brief_questions_to_jira,
    post_sync_note as _post_sync_note,
    upsert_jira_remote_link as _upsert_jira_remote_link,
    update_issue_sync_label as _update_issue_sync_label,
)
from orchestrator.core.jira_parent_child_sync_shared import (
    JiraParentChildSyncContext,
    build_parent_resync_prompt as _build_parent_resync_prompt,
    build_parent_seed_prompt as _build_parent_seed_prompt,
    combined_child_updates as _combined_child_updates,
    fanout_completion_note as _fanout_completion_note,
    material_parent_changed_fields as _material_parent_changed_fields,
    parent_board_entry_target_status as _parent_board_entry_target_status,
    pm_interview_jira_reply_scope as _pm_interview_jira_reply_scope,
    pm_interview_jira_transport as _pm_interview_jira_transport,
    project_key_for_issue as _project_key_for_issue,
    question_text as _question_text,
    sync_completion_note as _sync_completion_note,
)
from orchestrator.core.pm_interview_service import (
    PM_INTERVIEW_STATUS_PM_COMPLETED,
    PM_INTERVIEW_STATUS_QUESTION_PENDING,
    normalize_parent_feature_brief_with_runtime,
)
from orchestrator.core.parent_feature_workflow_operations import (
    PARENT_OP_BACKLOG_PLANNING,
    PARENT_OP_JIRA_CHILD_FANOUT,
    PARENT_OP_JIRA_COMMENT_PROJECTION,
    PARENT_OP_JIRA_PARENT_UPDATE,
    PARENT_PROJECTION_OPERATION_TYPES,
    PARENT_RETRYABLE_OPERATION_TYPES,
)
from orchestrator.core.parent_feature_planning_workflow import (
    ParentFeaturePlanningWorkflow,
    ParentFeaturePlanningWorkflowDeps,
)
from orchestrator.core.parent_planning_clarification_service import (
    ClarificationPublishEffects,
    ParentPlanningClarificationService,
)
from orchestrator.core.parent_planning_fanout_service import ParentPlanningFanoutSeedError, ParentPlanningFanoutService
from orchestrator.core.workflow_advance import (
    InvalidWorkflowOperationRetryError,
    UnsupportedWorkflowOperationRetryError,
)
from orchestrator.core.workflow_runtime import (
    WorkflowAdvanceOutcome,
    WorkflowAdvanceRequest,
)
from orchestrator.core.workflow_execution_projection import (
    WorkflowExecutionProjection,
    classify_external_workflow_failure,
    resolve_latest_issue_workflow,
)
from orchestrator.core.workflow_operation_service import (
    WorkflowOperationHandle,
    upsert_workflow_operation,
)
from orchestrator.tools.atlassian_oauth import JiraIssueDetail
from orchestrator.storage.models import Project, Tenant, WorkflowExecution, WorkflowOperation

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class _ParentWorkflowRetryContext:
    session: Session
    settings: Any
    workflow: WorkflowExecution
    operation: WorkflowOperation
    tenant: Tenant
    project: Project


@dataclass(frozen=True)
class ParentFeatureWorkflowHandlerDeps:
    integration_router: Any
    extract_changed_fields_fn: Any
    extract_status_transition_fn: Any
    build_runtime_for_selector_fn: Any
    seed_issues_with_runtime_fn: Any
    post_jira_comment_fn: Any
    create_jira_comment_fn: Any


class ParentFeatureWorkflowAdvanceHandler:
    def __init__(
        self,
        *,
        deps: ParentFeatureWorkflowHandlerDeps,
    ) -> None:
        self._deps = deps

    def advance(
        self,
        *,
        session: Session,
        settings,  # noqa: ANN001
        workflow_type,
        request: WorkflowAdvanceRequest,
        lifecycle,
    ) -> WorkflowAdvanceOutcome:
        context = JiraParentChildSyncContext(
            request_id=str(request.payload.get("request_id") or "").strip() or f"workflow-advance:{request.issue_key}",
            tenant_id=request.tenant_id,
            tenant=request.tenant,
            project_id=request.project_id,
            issue_key=request.issue_key,
            issue_labels=list(request.issue_labels),
            payload=dict(request.payload),
            webhook_event=request.webhook_event,
            comment_command=request.comment_command,
            comment_command_argument=request.comment_command_argument,
        )
        issue_gateway = _JiraParentIssueGateway(
            session=session,
            settings=settings,
            context=context,
            integration_router=self._deps.integration_router,
            post_jira_comment_fn=self._deps.post_jira_comment_fn,
            create_jira_comment_fn=self._deps.create_jira_comment_fn,
        )
        brief_planner = _ParentBriefPlanner(
            session=session,
            settings=settings,
            context=context,
            build_runtime_for_selector_fn=self._deps.build_runtime_for_selector_fn,
        )
        child_sync_gateway = _ParentChildSyncGateway(
            session=session,
            context=context,
            seed_issues_with_runtime_fn=self._deps.seed_issues_with_runtime_fn,
        )
        workflow = ParentFeaturePlanningWorkflow(
            deps=ParentFeaturePlanningWorkflowDeps(
                issue_gateway=issue_gateway,
                brief_planner=brief_planner,
                child_sync_gateway=child_sync_gateway,
                clarification_service=ParentPlanningClarificationService(),
                fanout_service=ParentPlanningFanoutService(),
                workflow_type=workflow_type,
                project_key_for_issue_fn=_project_key_for_issue,
                material_parent_changed_fields_fn=_material_parent_changed_fields,
                parent_board_entry_target_status_fn=_parent_board_entry_target_status,
                extract_changed_fields_fn=self._deps.extract_changed_fields_fn,
                extract_status_transition_fn=self._deps.extract_status_transition_fn,
            )
        )
        result = workflow.handle(
            context=context,
            session=session,
            settings=settings,
            lifecycle=lifecycle,
        )
        return result


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

    def _complete_backlog_planning_if_completed(
        self,
        *,
        lifecycle: WorkflowExecutionProjection,
        planning_result,
        backlog_planning_operation: WorkflowOperation,
    ) -> None:
        if planning_result.planning_state != PLANNING_STATE_COMPLETED:
            return
        if backlog_planning_operation.status == "completed":
            return
        planning_operation, planning_attempt = lifecycle.start_operation_attempt(
            operation_type=PARENT_OP_BACKLOG_PLANNING
        )
        lifecycle.complete_started_operation(
            operation=planning_operation,
            attempt=planning_attempt,
            summary="Backlog planning completed from the confirmed parent brief.",
        )

    def _retry_jira_parent_update(self, *, context: _ParentWorkflowRetryContext) -> WorkflowOperationHandle:
        jira_adapter = self._deps.integration_router.jira(
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
            raise InvalidWorkflowOperationRetryError(
                architecture_gate.block_reason or f"Architecture document link is required for {context.workflow.issue_key}"
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
                    raise InvalidWorkflowOperationRetryError(
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
        brief = resolve_parent_feature_brief(
            session=context.session,
            tenant_id=context.tenant.tenant_id,
            parent_issue_key=context.workflow.issue_key,
        )
        if brief is None:
            raise InvalidWorkflowOperationRetryError(
                f"No confirmed parent brief snapshot is available for {context.workflow.issue_key}"
            )
        jira_adapter = self._deps.integration_router.jira(
            session=context.session,
            tenant=context.tenant,
            settings=context.settings,
        )
        lifecycle = WorkflowExecutionProjection(session=context.session, workflow=context.workflow)
        operation, attempt = lifecycle.start_operation_attempt(operation_type=context.operation.operation_type)
        parent_detail = jira_adapter.get_issue_detail(issue_id_or_key=context.workflow.issue_key)
        sync_context = JiraParentChildSyncContext(
            request_id=f"workflow-operation:{operation.operation_id}",
            tenant_id=context.tenant.tenant_id,
            tenant=context.tenant,
            project_id=context.project.project_id,
            workflow_id=context.workflow.workflow_id,
            operation_id=operation.operation_id,
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
            build_runtime_for_selector_fn=self._deps.build_runtime_for_selector_fn,
        )
        child_sync_gateway = _ParentChildSyncGateway(
            session=context.session,
            context=sync_context,
            seed_issues_with_runtime_fn=self._deps.seed_issues_with_runtime_fn,
        )
        backlog_planning_operation = upsert_workflow_operation(
            context.session,
            workflow_id=context.workflow.workflow_id,
            operation_type=PARENT_OP_BACKLOG_PLANNING,
            idempotency_key=f"workflow-definition:{PARENT_OP_BACKLOG_PLANNING}",
            target_system=None,
            target_ref=None,
            summary="Backlog planning completed from the confirmed parent brief.",
        )
        fanout_service = ParentPlanningFanoutService()
        try:
            fanout = fanout_service.plan_and_seed(
                parent_detail=parent_detail,
                product_brief=brief.to_payload(),
                project_key=context.project.jira_project_key,
                planner=planner,
                child_sync_gateway=child_sync_gateway,
            )
        except ParentPlanningFanoutSeedError as exc:
            self._complete_backlog_planning_if_completed(
                lifecycle=lifecycle,
                planning_result=exc.planning_result,
                backlog_planning_operation=backlog_planning_operation,
            )
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
        self._complete_backlog_planning_if_completed(
            lifecycle=lifecycle,
            planning_result=fanout.planning_result,
            backlog_planning_operation=backlog_planning_operation,
        )
        if not fanout.completed:
            questions = fanout.questions
            clarification_service = ParentPlanningClarificationService()
            clarification_service.ensure_active_clarification(
                issue_key=context.workflow.issue_key,
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
                issue_key=context.workflow.issue_key,
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
        questions: tuple[ClarificationQuestion, ...],
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
        questions: tuple[ClarificationQuestion, ...],
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
            created_comment, error = _post_engineering_clarification_questions_to_jira(
                session=self._session,
                tenant=self._tenant,
                issue_key=issue_key,
                payload={},
                questions=questions,
                settings=self._settings,
                create_jira_comment_fn=self._create_jira_comment_fn,
            )
            if error is None and created_comment is not None:
                lifecycle = WorkflowExecutionProjection(session=self._session, workflow=self._workflow)
                operation, attempt = lifecycle.start_operation_attempt(operation_type=PARENT_OP_JIRA_COMMENT_PROJECTION)
                lifecycle.complete_started_operation(
                    operation=operation,
                    attempt=attempt,
                    summary="Posted engineering clarification questions to Jira.",
                )
        return ClarificationPublishEffects(
            state_recorded=True,
            jira_comment_created=error is None and created_comment is not None,
            discord_followup_created=False,
        )


class _JiraParentIssueGateway:
    def __init__(
        self,
        *,
        session: Session,
        settings,  # noqa: ANN001
        context: JiraParentChildSyncContext,
        integration_router,
        post_jira_comment_fn,
        create_jira_comment_fn,
    ) -> None:
        self._session = session
        self._settings = settings
        self._context = context
        self._integration_router = integration_router
        self._post_jira_comment_fn = post_jira_comment_fn
        self._create_jira_comment_fn = create_jira_comment_fn
        self._jira_adapter = None
        self._architecture_document_service = ArchitectureDocumentService(settings_factory=lambda: self._settings)

    def _jira(self):
        if self._jira_adapter is None:
            self._jira_adapter = self._integration_router.jira(
                session=self._session,
                tenant=self._context.tenant,
                settings=self._settings,
            )
        return self._jira_adapter

    def _oauth_context(self):
        jira = self._jira()
        return SimpleNamespace(
            client=jira.client,
            access_token=jira.access_token,
            connection=SimpleNamespace(cloud_id=jira.cloud_id, site_url=jira.site_url),
        )

    def _project(self) -> Project:
        project_id = str(self._context.project_id or "").strip()
        if not project_id:
            raise LookupError(f"No scoped project is available for Jira parent workflow {self._context.issue_key}")
        project = self._session.get(Project, project_id)
        if project is None or project.tenant_id != self._context.tenant_id:
            raise LookupError(f"Project {project_id} is not available for Jira parent workflow {self._context.issue_key}")
        return project

    def resolve_architecture_gate(
        self,
        *,
        parent_issue_key: str,
        issue_summary: str,
        issue_labels: list[str] | tuple[str, ...],
    ):
        return self._architecture_document_service.resolve_gate(
            session=self._session,
            project=self._project(),
            parent_issue_key=parent_issue_key,
            issue_summary=issue_summary,
            issue_labels=issue_labels,
            actor="system",
        )

    def load_parent_detail(self, issue_key: str) -> JiraIssueDetail:
        return self._jira().get_issue_detail(issue_id_or_key=issue_key)

    def load_child_details(self, *, project_key: str, parent_issue_key: str) -> list[JiraIssueDetail]:
        previews = self._jira().list_child_issue_previews(
            project_key=project_key,
            parent_issue_key=parent_issue_key,
        )
        return [
            self._jira().get_issue_detail(issue_id_or_key=preview.key)
            for preview in previews
        ]

    def update_issue_sync_label(self, *, issue_detail: JiraIssueDetail, target_label: str) -> None:
        _update_issue_sync_label(
            oauth=self._oauth_context(),
            issue_detail=issue_detail,
            target_label=target_label,
        )

    def upsert_architecture_document_link(
        self,
        *,
        issue_key: str,
        title: str,
        url: str,
    ) -> None:
        _upsert_jira_remote_link(
            oauth=self._oauth_context(),
            issue_key=issue_key,
            spec=architecture_document_remote_link_spec(
                issue_key=issue_key,
                title=title,
                url=url,
            ),
        )

    def upsert_workflow_execution_link(
        self,
        *,
        issue_key: str,
    ) -> None:
        workflow = resolve_latest_issue_workflow(
            session=self._session,
            tenant_id=self._context.tenant_id,
            issue_key=issue_key,
        )
        if workflow is None:
            return
        _upsert_jira_remote_link(
            oauth=self._oauth_context(),
            issue_key=issue_key,
            spec=workflow_execution_remote_link_spec(
                admin_ui_base_url=self._settings.admin_ui_base_url,
                workflow=workflow,
            ),
        )

    def post_parent_brief_questions(
        self,
        *,
        parent_issue_key: str,
        questions: tuple[ClarificationQuestion, ...],
    ) -> bool:
        return _post_parent_brief_questions_to_discord(
            session=self._session,
            settings=self._settings,
            tenant=self._context.tenant,
            project_id=self._context.project_id,
            parent_issue_key=parent_issue_key,
            questions=questions,
        )

    def post_parent_brief_questions_jira(
        self,
        *,
        parent_issue_key: str,
        questions: tuple[ClarificationQuestion, ...],
    ) -> tuple[dict[str, Any] | None, str | None]:
        created_comment, error = _post_parent_brief_questions_to_jira(
            session=self._session,
            tenant=self._context.tenant,
            project_id=self._context.project_id,
            issue_key=parent_issue_key,
            payload=dict(self._context.payload or {}),
            questions=questions,
            settings=self._settings,
            create_jira_comment_fn=self._create_jira_comment_fn,
        )
        return created_comment, error

    def has_active_clarification(
        self,
        *,
        issue_key: str,
        questions: tuple[ClarificationQuestion, ...],
    ) -> bool:
        return self.has_matching_active_pm_clarification_state(
            parent_issue_key=issue_key,
            questions=questions,
        )

    def publish_clarification(
        self,
        *,
        issue_key: str,
        questions: tuple[ClarificationQuestion, ...],
    ) -> ClarificationPublishEffects:
        posted_to_discord = self.post_parent_brief_questions(
            parent_issue_key=issue_key,
            questions=questions,
        )
        created_comment, error = self.post_parent_brief_questions_jira(
            parent_issue_key=issue_key,
            questions=questions,
        )
        if error is not None or created_comment is None:
            raise RuntimeError(
                f"Jira clarification projection failed for {issue_key}: {error or 'comment was not created'}"
            )
        jira_comment_created = error is None and created_comment is not None
        return ClarificationPublishEffects(
            state_recorded=True,
            jira_comment_created=jira_comment_created,
            discord_followup_created=posted_to_discord,
        )

    def has_matching_active_pm_clarification_state(
        self,
        *,
        parent_issue_key: str,
        questions: tuple[ClarificationQuestion, ...],
    ) -> bool:
        return has_matching_active_clarification_state(
            session=self._session,
            tenant_id=self._context.tenant_id,
            issue_key=parent_issue_key,
            context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
            questions=questions,
            transport=_pm_interview_jira_transport(),
            reply_scope=_pm_interview_jira_reply_scope(),
        )

    def post_sync_note(self, *, issue_key: str, body: str) -> None:
        _post_sync_note(
            session=self._session,
            tenant=self._context.tenant,
            issue_key=issue_key,
            settings=self._settings,
            body=body,
            post_jira_comment_fn=self._post_jira_comment_fn,
        )

    def mark_issues_sync_blocked(self, *, issue_keys: list[str]) -> None:
        _mark_issues_sync_blocked(oauth=self._oauth_context(), issue_keys=issue_keys)

    def transition_issue(self, *, issue_key: str, target_status: str) -> None:
        oauth = self._oauth_context()
        oauth.client.transition_issue(
            access_token=oauth.access_token,
            cloud_id=oauth.connection.cloud_id,
            issue_id_or_key=issue_key,
            target_status=target_status,
        )


def _jira_adapter(*, integration_router, session: Session, tenant, settings):  # noqa: ANN001
    return integration_router.jira(
        session=session,
        tenant=tenant,
        settings=settings,
    )


def _atlassian_oauth_context(*, integration_router, session: Session, tenant, settings):  # noqa: ANN001
    jira = _jira_adapter(
        integration_router=integration_router,
        session=session,
        tenant=tenant,
        settings=settings,
    )
    return SimpleNamespace(
        client=jira.client,
        access_token=jira.access_token,
        connection=SimpleNamespace(cloud_id=jira.cloud_id, site_url=jira.site_url),
    )


class _ParentBriefPlanner:
    def __init__(
        self,
        *,
        session: Session,
        settings,  # noqa: ANN001
        context: JiraParentChildSyncContext,
        build_runtime_for_selector_fn,
    ) -> None:
        self._session = session
        self._settings = settings
        self._context = context
        self._build_runtime_for_selector_fn = build_runtime_for_selector_fn

    def resolve_product_brief(
        self,
        *,
        parent_detail: JiraIssueDetail,
        refresh: bool,
    ) -> tuple[dict[str, object], list[str]]:
        return _resolve_parent_product_brief(
            session=self._session,
            settings=self._settings,
            tenant_id=self._context.tenant_id,
            project_id=self._context.project_id,
            parent_detail=parent_detail,
            build_runtime_for_selector_fn=self._build_runtime_for_selector_fn,
            workflow_id=self._context.workflow_id,
            operation_id=self._context.operation_id,
            refresh=refresh,
        )

    def plan_backlog_parent(
        self,
        *,
        parent_detail: JiraIssueDetail,
        product_brief: dict[str, object],
        project_key: str,
    ) -> tuple[object, dict[str, Any]]:
        planning_runtime = self._build_runtime_for_selector_fn(
            session=self._session,
            settings=self._settings,
            tenant_id=self._context.tenant_id,
            project_id=self._context.project_id,
            selector="workflow.pm_planning_architect",
        )
        planning_result = run_specialist_planning_fanout(
            session=self._session,
            settings=self._settings,
            runtime=planning_runtime,
            request=SpecialistPlanningRequest(
                tenant_id=self._context.tenant_id,
                project_id=self._context.project_id,
                parent_issue_key=parent_detail.key,
                parent_summary=parent_detail.summary,
                parent_description=parent_detail.description,
                product_brief=product_brief,
                project_keys=(project_key,),
                related_issues=(),
                status_counts={parent_detail.status: 1},
                github_context={},
                conversation_history=(),
                working_dir=".",
                workflow_id=self._context.workflow_id,
                operation_id=self._context.operation_id,
                attempt_id=self._context.attempt_id,
                attempt=self._context.attempt,
            ),
            runtime_for_selector=lambda selector: self._build_runtime_for_selector_fn(
                session=self._session,
                settings=self._settings,
                tenant_id=self._context.tenant_id,
                project_id=self._context.project_id,
                selector=selector,
            ),
        )
        planning_package = build_runtime_seed_planning_package(result=planning_result)
        return planning_result, planning_package


class _ParentChildSyncGateway:
    def __init__(
        self,
        *,
        session: Session,
        context: JiraParentChildSyncContext,
        seed_issues_with_runtime_fn,
    ) -> None:
        self._session = session
        self._context = context
        self._seed_issues_with_runtime_fn = seed_issues_with_runtime_fn

    def seed_parent_backlog_children(
        self,
        *,
        parent_detail: JiraIssueDetail,
        project_key: str,
        planning_package: dict[str, Any],
        planning_state: str,
    ) -> dict[str, Any]:
        _, seed_data = self._seed_issues_with_runtime_fn(
            session=self._session,
            tenant=self._context.tenant,
            prompt_markdown=_build_parent_seed_prompt(parent_detail=parent_detail),
            scoped_project_id=self._context.project_id,
            force_issue_keys=[self._context.issue_key],
            allow_create=True,
            allow_empty_children=planning_state != PLANNING_STATE_COMPLETED,
            scoped_project_keys=[project_key],
            codex_working_dir=".",
            planning_package=planning_package,
            workflow_id=self._context.workflow_id,
            operation_id=self._context.operation_id,
            attempt_ref=WorkflowAttemptRef(
                number=self._context.attempt,
                attempt_id=self._context.attempt_id,
            ),
        )
        return seed_data

    def refresh_parent_children(
        self,
        *,
        parent_detail: JiraIssueDetail,
        child_details: list[JiraIssueDetail],
        changed_fields: list[str],
        project_key: str,
    ) -> dict[str, Any]:
        _, seed_data = self._seed_issues_with_runtime_fn(
            session=self._session,
            tenant=self._context.tenant,
            prompt_markdown=_build_parent_resync_prompt(
                parent_detail=parent_detail,
                child_details=child_details,
                changed_fields=changed_fields,
            ),
            scoped_project_id=self._context.project_id,
            force_issue_keys=[self._context.issue_key, *[detail.key for detail in child_details]],
            allow_create=True,
            allow_empty_children=True,
            scoped_project_keys=[project_key],
            codex_working_dir=".",
            workflow_id=self._context.workflow_id,
            operation_id=self._context.operation_id,
            attempt_ref=WorkflowAttemptRef(
                number=self._context.attempt,
                attempt_id=self._context.attempt_id,
            ),
        )
        return seed_data

    @staticmethod
    def combined_child_updates(*, seed_data: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
        return _combined_child_updates(seed_data=seed_data)

    @staticmethod
    def sync_completion_note(*, updated_children: list[str], created_children: list[str]) -> str:
        return _sync_completion_note(updated_children=updated_children, created_children=created_children)

    @staticmethod
    def fanout_completion_note(
        *,
        target_status: str,
        promoted_children: list[str],
        unchanged_children: list[str],
        skipped_children: list[str],
        failed_children: list[str],
    ) -> str:
        return _fanout_completion_note(
            target_status=target_status,
            promoted_children=promoted_children,
            unchanged_children=unchanged_children,
            skipped_children=skipped_children,
            failed_children=failed_children,
        )


def _resolve_parent_product_brief(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant_id: str,
    project_id: str | None,
    parent_detail: JiraIssueDetail,
    build_runtime_for_selector_fn,
    workflow_id: str | None = None,
    operation_id: str | None = None,
    refresh: bool = False,
) -> tuple[dict[str, object], list[object]]:
    canonical_brief = None if refresh else resolve_parent_feature_brief(
        session=session,
        tenant_id=tenant_id,
        parent_issue_key=parent_detail.key,
    )
    if canonical_brief is not None:
        return canonical_brief.to_payload(), []
    runtime = build_runtime_for_selector_fn(
        session=session,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
        selector="workflow.pm_parent_brief_normalization",
    )
    normalization = normalize_parent_feature_brief_with_runtime(
        session=session,
        settings=settings,
        runtime=runtime,
        parent_issue_key=parent_detail.key,
        parent_summary=parent_detail.summary,
        parent_description=parent_detail.description,
        invocation_context=AgentInvocationContext(
            channel="jira",
            tenant_id=tenant_id,
            project_id=project_id,
            command="pm",
            stage="pm_parent_brief_normalization",
            working_dir=".",
            workflow_id=workflow_id,
            operation_id=operation_id,
            issue_key=parent_detail.key,
        ),
    )
    brief_payload = dict(normalization.get("brief") or {})
    open_questions = [value for value in normalization.get("open_questions", []) if _question_text(value)]
    persist_parent_feature_brief_snapshot(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        parent_issue_key=parent_detail.key,
        source_text=parent_detail.description,
        brief=brief_payload,
        status=PM_INTERVIEW_STATUS_QUESTION_PENDING if open_questions else PM_INTERVIEW_STATUS_PM_COMPLETED,
        notes={
            "source": "jira_parent_brief_normalization",
            "parent_summary": parent_detail.summary,
        },
    )
    return brief_payload, open_questions
