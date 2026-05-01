from __future__ import annotations

from orchestrator.core.parent_feature_brief_store import resolve_parent_feature_brief
from orchestrator.core.parent_feature_workflow.adapters import _ParentBriefPlanner
from orchestrator.core.parent_feature_workflow.dependencies import ParentFeatureWorkflowHandlerDeps
from orchestrator.core.parent_feature_workflow.operations import PARENT_OP_JIRA_CHILD_FANOUT
from orchestrator.core.parent_feature_workflow.retry_support import (
    ParentWorkflowRetryContext,
    child_sync_context,
    execute_child_fanout_on_started_attempt,
    jira_issue_key_for_workflow,
    operation_handle,
    product_escalation_questions,
    wait_for_stakeholder_clarification,
)
from orchestrator.core.specialist_planning import PLANNING_STATE_COMPLETED, RetryableSpecialistPlanningContractError
from orchestrator.core.workflow_execution_projection import WorkflowExecutionProjection, classify_external_workflow_failure
from orchestrator.core.workflow_operation_service import WorkflowOperationHandle


class JiraChildFanoutRetryExecutor:
    operation_type = PARENT_OP_JIRA_CHILD_FANOUT

    def __init__(self, *, deps: ParentFeatureWorkflowHandlerDeps) -> None:
        self._deps = deps

    def execute(self, *, context: ParentWorkflowRetryContext) -> WorkflowOperationHandle:
        parent_issue_key = jira_issue_key_for_workflow(context.workflow)
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
        operation, attempt = lifecycle.start_operation_attempt(operation_type=context.operation.operation_type)
        if brief is None:
            lifecycle.fail_started_operation(
                operation=operation,
                attempt=attempt,
                category="missing_input",
                message=f"No confirmed parent brief snapshot is available for {parent_issue_key}",
            )
            return operation_handle(context=context, operation=operation)
        jira_adapter = self._deps.integration_router.jira(
            session=context.session,
            tenant=context.tenant,
            settings=context.settings,
        )
        parent_detail = jira_adapter.get_issue_detail(issue_id_or_key=parent_issue_key)
        planner = _ParentBriefPlanner(
            session=context.session,
            settings=context.settings,
            context=child_sync_context(
                context=context,
                operation=operation,
                attempt=attempt,
                parent_issue_key=parent_issue_key,
                parent_detail=parent_detail,
            ),
            build_runtime_for_selector_fn=self._deps.build_runtime_for_selector_fn,
        )
        brief_payload = brief.to_payload()
        try:
            planning_result, planning_package = planner.plan_backlog_parent(
                parent_detail=parent_detail,
                product_brief=brief_payload,
                project_key=context.project.jira_project_key,
            )
            if planning_result.planning_state != PLANNING_STATE_COMPLETED:
                wait_for_stakeholder_clarification(
                    context=context,
                    deps=self._deps,
                    lifecycle=lifecycle,
                    operation=operation,
                    attempt=attempt,
                    parent_issue_key=parent_issue_key,
                    questions=product_escalation_questions(planning_result),
                    clarification_context="Engineering child fanout",
                )
                return operation_handle(context=context, operation=operation)
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
            return operation_handle(context=context, operation=operation)

        return execute_child_fanout_on_started_attempt(
            context=context,
            deps=self._deps,
            lifecycle=lifecycle,
            operation=operation,
            attempt=attempt,
            parent_issue_key=parent_issue_key,
            parent_detail=parent_detail,
            planning_result=planning_result,
            planning_package=planning_package,
        )
