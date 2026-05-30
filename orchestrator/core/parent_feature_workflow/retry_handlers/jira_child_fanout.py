from __future__ import annotations

from orchestrator.core.projects.parent_feature_brief_store import resolve_parent_feature_brief
from orchestrator.core.parent_feature_workflow.adapters import _ParentBriefPlanner
from orchestrator.core.parent_feature_workflow.dependencies import ParentFeatureWorkflowHandlerDeps
from orchestrator.core.parent_feature_workflow.operations import PARENT_OP_JIRA_CHILD_FANOUT
from orchestrator.core.parent_feature_workflow.operations import PARENT_OP_PM_DECISION_RESOLUTION
from orchestrator.core.parent_feature_workflow.retry_support import (
    ParentWorkflowRetryContext,
    augment_product_brief_with_pm_resolution,
    child_sync_context,
    execute_child_fanout_on_started_attempt,
    jira_issue_key_for_workflow,
    operation_handle,
    record_specialist_decisions_for_retry,
    stakeholder_escalation_questions,
    wait_for_stakeholder_clarification,
)
from orchestrator.core.planning.decision_records import PlanningDecisionRecordStore
from orchestrator.core.planning.specialist import PLANNING_STATE_COMPLETED, RetryableSpecialistPlanningContractError
from orchestrator.core.workflow.execution_projection import WorkflowExecutionProjection, classify_external_workflow_failure
from orchestrator.core.workflow.operation_service import WorkflowOperationHandle


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
            record_specialist_decisions_for_retry(
                context=context,
                operation=operation,
                attempt=attempt,
                parent_issue_key=parent_issue_key,
                planning_result=planning_result,
            )
            if getattr(planning_result, "pm_decision_requests", ()) or ():
                pm_operation, pm_attempt = lifecycle.start_operation_attempt(operation_type=PARENT_OP_PM_DECISION_RESOLUTION)
                pm_planner = _ParentBriefPlanner(
                    session=context.session,
                    settings=context.settings,
                    context=child_sync_context(
                        context=context,
                        operation=pm_operation,
                        attempt=pm_attempt,
                        parent_issue_key=parent_issue_key,
                        parent_detail=parent_detail,
                    ),
                    build_runtime_for_selector_fn=self._deps.build_runtime_for_selector_fn,
                )
                try:
                    pm_resolution = pm_planner.resolve_pm_decisions(
                        parent_detail=parent_detail,
                        product_brief=brief_payload,
                        planning_result=planning_result,
                        planning_package=planning_package,
                    )
                    decision_store = PlanningDecisionRecordStore(session=context.session)
                    decision_store.record_pm_resolutions(
                        tenant_id=context.tenant.tenant_id,
                        project_id=context.project.project_id,
                        workflow_id=context.workflow.workflow_id,
                        source_operation_id=pm_operation.operation_id,
                        source_attempt_id=pm_attempt.attempt_id,
                        parent_issue_key=parent_issue_key,
                        resolutions=tuple(getattr(pm_resolution, "resolved_decisions", ()) or ()),
                    )
                    decision_store.record_stakeholder_escalations(
                        tenant_id=context.tenant.tenant_id,
                        project_id=context.project.project_id,
                        workflow_id=context.workflow.workflow_id,
                        source_operation_id=pm_operation.operation_id,
                        source_attempt_id=pm_attempt.attempt_id,
                        parent_issue_key=parent_issue_key,
                        escalations=tuple(getattr(pm_resolution, "stakeholder_escalations", ()) or ()),
                    )
                except Exception as exc:  # noqa: BLE001
                    lifecycle.fail_started_operation(
                        operation=pm_operation,
                        attempt=pm_attempt,
                        category=classify_external_workflow_failure(error=exc),
                        message=str(exc),
                    )
                    raise
                stakeholder_questions = stakeholder_escalation_questions(pm_resolution)
                if stakeholder_questions:
                    lifecycle.complete_started_operation(
                        operation=pm_operation,
                        attempt=pm_attempt,
                        summary="PM escalated stakeholder-owned product clarification.",
                    )
                    wait_for_stakeholder_clarification(
                        context=context,
                        deps=self._deps,
                        lifecycle=lifecycle,
                        operation=operation,
                        attempt=attempt,
                        parent_issue_key=parent_issue_key,
                        questions=stakeholder_questions,
                        clarification_context="PM decision resolution",
                    )
                    return operation_handle(context=context, operation=operation)
                lifecycle.complete_started_operation(
                    operation=pm_operation,
                    attempt=pm_attempt,
                    summary="PM resolved specialist product decision requests internally.",
                )
                brief_payload = augment_product_brief_with_pm_resolution(
                    product_brief=brief_payload,
                    pm_resolution=pm_resolution,
                )
                planning_result, planning_package = planner.plan_backlog_parent(
                    parent_detail=parent_detail,
                    product_brief=brief_payload,
                    project_key=context.project.jira_project_key,
                )
                record_specialist_decisions_for_retry(
                    context=context,
                    operation=operation,
                    attempt=attempt,
                    parent_issue_key=parent_issue_key,
                    planning_result=planning_result,
                )
                if getattr(planning_result, "pm_decision_requests", ()) or ():
                    lifecycle.fail_started_operation(
                        operation=operation,
                        attempt=attempt,
                        category="invalid_model_output",
                        message="Specialist planning returned PM decision requests after PM decision resolution.",
                    )
                    return operation_handle(context=context, operation=operation)
            if planning_result.planning_state != PLANNING_STATE_COMPLETED:
                lifecycle.fail_started_operation(
                    operation=operation,
                    attempt=attempt,
                    category="invalid_model_output",
                    message="Specialist planning blocked without PM decision requests.",
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
            product_brief=brief_payload,
            planning_result=planning_result,
            planning_package=planning_package,
        )
