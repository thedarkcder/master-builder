from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.projects.architecture_document_service import (
    ArchitectureDocumentService,
)
from orchestrator.core.clarification.questions import ClarificationQuestion
from orchestrator.core.clarification.projection_service import (
    matching_active_jira_clarification_evidence_id,
)
from orchestrator.core.pm.followup_context_service import FOLLOWUP_CONTEXT_PM_INTERVIEW
from orchestrator.core.integrations.atlassian.links import (
    architecture_document_remote_link_spec,
    workflow_execution_remote_link_spec,
)
from orchestrator.core.integrations.atlassian.parent_child_sync_publishers import (
    mark_issues_sync_blocked as _mark_issues_sync_blocked,
    post_parent_brief_questions_to_discord as _post_parent_brief_questions_to_discord,
    post_parent_brief_questions_to_jira as _post_parent_brief_questions_to_jira,
    post_start_development_link_to_jira as _post_start_development_link_to_jira,
    post_sync_note as _post_sync_note,
    upsert_jira_remote_link as _upsert_jira_remote_link,
    update_issue_sync_label as _update_issue_sync_label,
)
from orchestrator.core.integrations.atlassian.parent_child_sync_shared import (
    JiraParentChildSyncContext,
    build_parent_resync_prompt as _build_parent_resync_prompt,
    build_parent_seed_prompt as _build_parent_seed_prompt,
    combined_child_updates as _combined_child_updates,
    fanout_completion_note as _fanout_completion_note,
    pm_interview_jira_reply_scope as _pm_interview_jira_reply_scope,
    pm_interview_jira_transport as _pm_interview_jira_transport,
    extract_created_comment_id as _extract_created_comment_id,
    question_text as _question_text,
    sync_completion_note as _sync_completion_note,
)
from orchestrator.core.projects.parent_feature_brief_store import (
    parent_planning_clarification_history,
    persist_parent_feature_brief_snapshot,
    resolve_parent_feature_brief,
)
from orchestrator.core.projects.parent_planning_clarification_service import (
    ClarificationPublishEffects,
)
from orchestrator.core.pm.interview_service import (
    PM_INTERVIEW_STATUS_PM_COMPLETED,
    PM_INTERVIEW_STATUS_QUESTION_PENDING,
    normalize_parent_feature_brief_with_runtime,
)
from orchestrator.core.runtime.invocation import AgentInvocationContext
from orchestrator.core.workflow.attempt_ref import WorkflowAttemptRef
from orchestrator.core.planning.specialist import (
    PLANNING_STATE_COMPLETED,
    PLANNING_STATE_ENGINEERING,
    PLANNING_STATE_SECURITY,
    PLANNING_STATE_TEST,
    SpecialistPlanningRequest,
    build_runtime_seed_planning_package,
    run_specialist_planning_fanout,
)
from orchestrator.core.planning.pm_decision_resolution import (
    PMDecisionResolutionRequest,
    PMDecisionResolutionService,
)
from orchestrator.core.parent_feature_workflow.operations import (
    PARENT_OP_BACKLOG_PLANNING,
    PARENT_OP_JIRA_CHILD_FANOUT,
    PARENT_WU_BACKLOG_ARCHITECTURE_MODEL,
    PARENT_WU_BACKLOG_PACKAGE_ASSEMBLY,
    PARENT_WU_BACKLOG_SECURITY_MODEL,
    PARENT_WU_BACKLOG_TESTING_MODEL,
    PARENT_WU_JIRA_CHILD_FANOUT_ARCHITECTURE_MODEL,
    PARENT_WU_JIRA_CHILD_FANOUT_PACKAGE_ASSEMBLY,
    PARENT_WU_JIRA_CHILD_FANOUT_SECURITY_MODEL,
    PARENT_WU_JIRA_CHILD_FANOUT_SEED,
    PARENT_WU_JIRA_CHILD_FANOUT_TESTING_MODEL,
)
from orchestrator.core.workflow.execution_projection import (
    resolve_latest_workflow_execution_by_source,
)
from orchestrator.core.workflow.work_units import (
    run_work_unit,
    workflow_work_unit_input_fingerprint,
)
from orchestrator.storage.models import (
    Project,
    WorkflowOperation,
    WorkflowOperationAttempt,
)
from orchestrator.tools.atlassian_oauth import JiraIssueDetail

logger = logging.getLogger(__name__)


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
        self._architecture_document_service = ArchitectureDocumentService(
            settings_factory=lambda: self._settings
        )

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
            raise LookupError(
                f"No scoped project is available for Jira parent workflow {self._context.issue_key}"
            )
        project = self._session.get(Project, project_id)
        if project is None or project.tenant_id != self._context.tenant_id:
            raise LookupError(
                f"Project {project_id} is not available for Jira parent workflow {self._context.issue_key}"
            )
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

    def load_issue_detail(self, issue_key: str) -> JiraIssueDetail:
        return self.load_parent_detail(issue_key)

    def load_child_details(
        self, *, project_key: str, parent_issue_key: str
    ) -> list[JiraIssueDetail]:
        previews = self._jira().list_child_issue_previews(
            project_key=project_key,
            parent_issue_key=parent_issue_key,
        )
        return [
            self._jira().get_issue_detail(issue_id_or_key=preview.key)
            for preview in previews
        ]

    def list_project_issue_types_for_create(self, *, project_key: str) -> list[str]:
        jira = self._jira()
        return jira.client.list_project_issue_types_for_create(
            access_token=jira.access_token,
            cloud_id=jira.cloud_id,
            project_key=project_key,
        )

    def update_issue_sync_label(
        self, *, issue_detail: JiraIssueDetail, target_label: str
    ) -> None:
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
        workflow = resolve_latest_workflow_execution_by_source(
            session=self._session,
            tenant_id=self._context.tenant_id,
            source_system="jira",
            source_ref=issue_key,
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

    def active_clarification_effects(
        self,
        *,
        issue_key: str,
        questions: tuple[ClarificationQuestion, ...],
    ) -> ClarificationPublishEffects | None:
        jira_comment_id = self.matching_active_pm_jira_comment_id(
            parent_issue_key=issue_key,
            questions=questions,
        )
        if jira_comment_id is None:
            return None
        comments = self._jira().list_issue_comments(issue_id_or_key=issue_key)
        if not any(
            str(getattr(comment, "comment_id", "") or "").strip() == jira_comment_id
            for comment in comments
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
        questions: tuple[ClarificationQuestion, ...],
    ) -> ClarificationPublishEffects:
        created_comment, error = self.post_parent_brief_questions_jira(
            parent_issue_key=issue_key,
            questions=questions,
        )
        if error is not None or created_comment is None:
            raise RuntimeError(
                f"Jira clarification projection failed for {issue_key}: {error or 'comment was not created'}"
            )
        jira_comment_id = _extract_created_comment_id(created_comment)
        if not jira_comment_id:
            raise RuntimeError(
                f"Jira clarification projection for {issue_key} did not return a comment id"
            )
        jira_comment_created = error is None and created_comment is not None
        posted_to_discord = False
        try:
            posted_to_discord = self.post_parent_brief_questions(
                parent_issue_key=issue_key,
                questions=questions,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "optional_discord_clarification_projection_failed issue_key=%s",
                issue_key,
                exc_info=True,
            )
        return ClarificationPublishEffects(
            state_recorded=True,
            jira_comment_created=jira_comment_created,
            discord_followup_created=posted_to_discord,
            jira_comment_id=jira_comment_id,
        )

    def publish_start_development_link(
        self, *, issue_key: str, action_url: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        return _post_start_development_link_to_jira(
            session=self._session,
            tenant=self._context.tenant,
            issue_key=issue_key,
            action_url=action_url,
            settings=self._settings,
            create_jira_comment_fn=self._create_jira_comment_fn,
        )

    def matching_active_pm_jira_comment_id(
        self,
        *,
        parent_issue_key: str,
        questions: tuple[ClarificationQuestion, ...],
    ) -> str | None:
        return matching_active_jira_clarification_evidence_id(
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

    def with_attempt(
        self,
        *,
        attempt_ref: WorkflowAttemptRef,
    ) -> _ParentBriefPlanner:
        return _ParentBriefPlanner(
            session=self._session,
            settings=self._settings,
            context=replace(
                self._context,
                workflow_id=attempt_ref.require_workflow_id(),
                operation_id=attempt_ref.require_operation_id(),
                attempt=attempt_ref.number,
                attempt_id=attempt_ref.require_attempt_id(),
            ),
            build_runtime_for_selector_fn=self._build_runtime_for_selector_fn,
        )

    def _attempt_models(self) -> tuple[WorkflowOperation, WorkflowOperationAttempt]:
        operation_id = str(self._context.operation_id or "").strip()
        attempt_id = str(self._context.attempt_id or "").strip()
        if not operation_id or not attempt_id:
            raise RuntimeError(
                "Parent brief planner work units require operation_id and attempt_id"
            )
        operation = self._session.get(WorkflowOperation, operation_id)
        attempt = self._session.get(WorkflowOperationAttempt, attempt_id)
        if operation is None:
            raise RuntimeError(
                f"Workflow operation {operation_id} is missing for parent brief planner"
            )
        if attempt is None:
            raise RuntimeError(
                f"Workflow operation attempt {attempt_id} is missing for parent brief planner"
            )
        return operation, attempt

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
            attempt=self._context.attempt,
            attempt_id=self._context.attempt_id,
            refresh=refresh,
        )

    def _package_assembly_unit_key(self, *, operation_type: str) -> str:
        if operation_type == PARENT_OP_BACKLOG_PLANNING:
            return PARENT_WU_BACKLOG_PACKAGE_ASSEMBLY
        if operation_type == PARENT_OP_JIRA_CHILD_FANOUT:
            return PARENT_WU_JIRA_CHILD_FANOUT_PACKAGE_ASSEMBLY
        raise RuntimeError(
            f"No planning package assembly work unit is declared for {operation_type}"
        )

    def _stage_work_unit_keys(self, *, operation_type: str) -> dict[str, str]:
        if operation_type == PARENT_OP_BACKLOG_PLANNING:
            return {
                PLANNING_STATE_ENGINEERING: PARENT_WU_BACKLOG_ARCHITECTURE_MODEL,
                PLANNING_STATE_SECURITY: PARENT_WU_BACKLOG_SECURITY_MODEL,
                PLANNING_STATE_TEST: PARENT_WU_BACKLOG_TESTING_MODEL,
            }
        if operation_type == PARENT_OP_JIRA_CHILD_FANOUT:
            return {
                PLANNING_STATE_ENGINEERING: PARENT_WU_JIRA_CHILD_FANOUT_ARCHITECTURE_MODEL,
                PLANNING_STATE_SECURITY: PARENT_WU_JIRA_CHILD_FANOUT_SECURITY_MODEL,
                PLANNING_STATE_TEST: PARENT_WU_JIRA_CHILD_FANOUT_TESTING_MODEL,
            }
        raise RuntimeError(
            f"No specialist planning work units are declared for {operation_type}"
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
                conversation_history=parent_planning_clarification_history(
                    session=self._session,
                    tenant_id=self._context.tenant_id,
                    parent_issue_key=parent_detail.key,
                ),
                working_dir=".",
                workflow_id=self._context.workflow_id,
                operation_id=self._context.operation_id,
                attempt_id=self._context.attempt_id,
                attempt=self._context.attempt,
                work_unit_keys_by_stage=self._stage_work_unit_keys(
                    operation_type=self._attempt_models()[0].operation_type,
                ),
            ),
            runtime_for_selector=lambda selector: self._build_runtime_for_selector_fn(
                session=self._session,
                settings=self._settings,
                tenant_id=self._context.tenant_id,
                project_id=self._context.project_id,
                selector=selector,
            ),
        )
        operation, attempt = self._attempt_models()
        assembly_input = {"planning_result": planning_result.to_payload()}
        assembly_hash = workflow_work_unit_input_fingerprint(assembly_input)
        planning_package = run_work_unit(
            self._session,
            operation=operation,
            operation_attempt=attempt,
            unit_key=self._package_assembly_unit_key(
                operation_type=operation.operation_type
            ),
            idempotency_key=f"{parent_detail.key}:planning_package:{operation.operation_type}:{assembly_hash}",
            input_payload=assembly_input,
            execute=lambda _context: build_runtime_seed_planning_package(
                result=planning_result
            ),
            serialize=lambda result: {"planning_package": result},
            deserialize=lambda payload: dict(payload.get("planning_package") or {}),
        )
        return planning_result, planning_package

    def resolve_pm_decisions(
        self,
        *,
        parent_detail: JiraIssueDetail,
        product_brief: dict[str, object],
        planning_result,
        planning_package: dict[str, Any],
    ):
        resolution_runtime = self._build_runtime_for_selector_fn(
            session=self._session,
            settings=self._settings,
            tenant_id=self._context.tenant_id,
            project_id=self._context.project_id,
            selector="workflow.pm_decision_resolution",
        )
        return PMDecisionResolutionService().resolve(
            session=self._session,
            settings=self._settings,
            runtime=resolution_runtime,
            request=PMDecisionResolutionRequest(
                tenant_id=self._context.tenant_id,
                project_id=self._context.project_id,
                parent_issue_key=parent_detail.key,
                parent_summary=parent_detail.summary,
                parent_description=parent_detail.description,
                product_brief=dict(product_brief),
                planning_package=planning_package,
                technical_decisions=tuple(
                    getattr(planning_result, "technical_decisions", ()) or ()
                ),
                pm_decision_requests=tuple(
                    getattr(planning_result, "pm_decision_requests", ()) or ()
                ),
                conversation_history=tuple(
                    parent_planning_clarification_history(
                        session=self._session,
                        tenant_id=self._context.tenant_id,
                        parent_issue_key=parent_detail.key,
                    )
                ),
                workflow_id=self._context.workflow_id,
                operation_id=self._context.operation_id,
                attempt_id=self._context.attempt_id,
                attempt=self._context.attempt,
            ),
        )


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

    def with_attempt(
        self,
        *,
        attempt_ref: WorkflowAttemptRef,
    ) -> _ParentChildSyncGateway:
        return _ParentChildSyncGateway(
            session=self._session,
            context=replace(
                self._context,
                workflow_id=attempt_ref.require_workflow_id(),
                operation_id=attempt_ref.require_operation_id(),
                attempt=attempt_ref.number,
                attempt_id=attempt_ref.require_attempt_id(),
            ),
            seed_issues_with_runtime_fn=self._seed_issues_with_runtime_fn,
        )

    def _attempt_models(self) -> tuple[WorkflowOperation, WorkflowOperationAttempt]:
        operation_id = str(self._context.operation_id or "").strip()
        attempt_id = str(self._context.attempt_id or "").strip()
        if not operation_id or not attempt_id:
            raise RuntimeError(
                "Parent child sync work units require operation_id and attempt_id"
            )
        operation = self._session.get(WorkflowOperation, operation_id)
        attempt = self._session.get(WorkflowOperationAttempt, attempt_id)
        if operation is None:
            raise RuntimeError(
                f"Workflow operation {operation_id} is missing for parent child sync"
            )
        if attempt is None:
            raise RuntimeError(
                f"Workflow operation attempt {attempt_id} is missing for parent child sync"
            )
        return operation, attempt

    def seed_parent_backlog_children(
        self,
        *,
        parent_detail: JiraIssueDetail,
        project_key: str,
        planning_package: dict[str, Any],
        planning_state: str,
    ) -> dict[str, Any]:
        operation, attempt = self._attempt_models()

        def _execute(_context) -> dict[str, Any]:  # noqa: ANN001
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
                    workflow_id=self._context.workflow_id,
                    operation_id=self._context.operation_id,
                    attempt_id=self._context.attempt_id,
                    number=self._context.attempt,
                ),
            )
            return seed_data

        input_payload = {
            "parent_issue_key": parent_detail.key,
            "project_key": project_key,
            "planning_package": planning_package,
            "planning_state": planning_state,
        }
        input_hash = workflow_work_unit_input_fingerprint(input_payload)
        return run_work_unit(
            self._session,
            operation=operation,
            operation_attempt=attempt,
            unit_key=PARENT_WU_JIRA_CHILD_FANOUT_SEED,
            idempotency_key=f"{parent_detail.key}:seed_parent_backlog_children:{input_hash}",
            input_payload=input_payload,
            execute=_execute,
            serialize=lambda result: {"seed_data": result},
            deserialize=lambda payload: dict(payload.get("seed_data") or {}),
        )

    def refresh_parent_children(
        self,
        *,
        parent_detail: JiraIssueDetail,
        child_details: list[JiraIssueDetail],
        changed_fields: list[str],
        project_key: str,
    ) -> dict[str, Any]:
        operation, attempt = self._attempt_models()

        def _execute(_context) -> dict[str, Any]:  # noqa: ANN001
            _, seed_data = self._seed_issues_with_runtime_fn(
                session=self._session,
                tenant=self._context.tenant,
                prompt_markdown=_build_parent_resync_prompt(
                    parent_detail=parent_detail,
                    child_details=child_details,
                    changed_fields=changed_fields,
                ),
                scoped_project_id=self._context.project_id,
                force_issue_keys=[
                    self._context.issue_key,
                    *[detail.key for detail in child_details],
                ],
                allow_create=True,
                allow_empty_children=True,
                scoped_project_keys=[project_key],
                codex_working_dir=".",
                workflow_id=self._context.workflow_id,
                operation_id=self._context.operation_id,
                attempt_ref=WorkflowAttemptRef(
                    workflow_id=self._context.workflow_id,
                    operation_id=self._context.operation_id,
                    attempt_id=self._context.attempt_id,
                    number=self._context.attempt,
                ),
            )
            return seed_data

        input_payload = {
            "parent_issue_key": parent_detail.key,
            "child_issue_keys": [detail.key for detail in child_details],
            "changed_fields": list(changed_fields),
            "project_key": project_key,
        }
        input_hash = workflow_work_unit_input_fingerprint(input_payload)
        return run_work_unit(
            self._session,
            operation=operation,
            operation_attempt=attempt,
            unit_key=PARENT_WU_JIRA_CHILD_FANOUT_SEED,
            idempotency_key=f"{parent_detail.key}:refresh_parent_children:{input_hash}",
            input_payload=input_payload,
            execute=_execute,
            serialize=lambda result: {"seed_data": result},
            deserialize=lambda payload: dict(payload.get("seed_data") or {}),
        )

    @staticmethod
    def combined_child_updates(
        *, seed_data: dict[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        return _combined_child_updates(seed_data=seed_data)

    @staticmethod
    def sync_completion_note(
        *, updated_children: list[str], created_children: list[str]
    ) -> str:
        return _sync_completion_note(
            updated_children=updated_children, created_children=created_children
        )

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
    attempt: int | None = None,
    attempt_id: str | None = None,
    refresh: bool = False,
) -> tuple[dict[str, object], list[object]]:
    canonical_brief = (
        None
        if refresh
        else resolve_parent_feature_brief(
            session=session,
            tenant_id=tenant_id,
            parent_issue_key=parent_detail.key,
        )
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
            attempt=attempt,
            attempt_id=attempt_id,
            issue_key=parent_detail.key,
            db_session=session,
        ),
    )
    brief_payload = dict(normalization.get("brief") or {})
    open_questions = [
        value
        for value in normalization.get("open_questions", [])
        if _question_text(value)
    ]
    persist_parent_feature_brief_snapshot(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        parent_issue_key=parent_detail.key,
        source_text=parent_detail.description,
        brief=brief_payload,
        status=PM_INTERVIEW_STATUS_QUESTION_PENDING
        if open_questions
        else PM_INTERVIEW_STATUS_PM_COMPLETED,
        notes={
            "source": "jira_parent_brief_normalization",
            "parent_summary": parent_detail.summary,
        },
    )
    return brief_payload, open_questions
