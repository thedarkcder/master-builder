from __future__ import annotations
import logging

from sqlalchemy.orm import Session

from orchestrator.core.jira_parent_child_sync_shared import (
    JiraParentChildSyncContext,
    material_parent_changed_fields as _material_parent_changed_fields,
    parent_board_entry_target_status as _parent_board_entry_target_status,
    project_key_for_issue as _project_key_for_issue,
)
from orchestrator.core.parent_feature_workflow.adapters import _JiraParentIssueGateway, _ParentBriefPlanner, _ParentChildSyncGateway
from orchestrator.core.parent_feature_workflow.dependencies import ParentFeatureWorkflowHandlerDeps
from orchestrator.core.parent_feature_workflow.planning import (
    ParentFeaturePlanningWorkflow,
    ParentFeaturePlanningWorkflowDeps,
)
from orchestrator.core.parent_planning_clarification_service import ParentPlanningClarificationService
from orchestrator.core.parent_planning_fanout_service import ParentPlanningFanoutService
from orchestrator.core.workflow_runtime import (
    WorkflowAdvanceOutcome,
    WorkflowAdvanceRequest,
)

logger = logging.getLogger(__name__)


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
