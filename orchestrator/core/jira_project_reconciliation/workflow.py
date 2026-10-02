from __future__ import annotations

from orchestrator.core.workflow.definition import (
    WorkflowStepKind,
    WorkflowWorkUnitIdempotencyPolicy,
    WorkflowWorkUnitKind,
    WorkflowWorkUnitRetryPolicy,
    workflow_step,
    workflow_work_unit,
)

JIRA_PROJECT_RECONCILIATION_STEP_SCAN = "jira_project_scan"
JIRA_PROJECT_RECONCILIATION_STEP_CLASSIFICATION = "jira_issue_classification"
JIRA_PROJECT_RECONCILIATION_STEP_LABEL_RECONCILIATION = "jira_label_reconciliation"
JIRA_PROJECT_RECONCILIATION_STEP_PARENT_RECONCILIATION = (
    "parent_workflow_reconciliation"
)
JIRA_PROJECT_RECONCILIATION_STEP_SUMMARY = "reconciliation_summary"

JIRA_PROJECT_RECONCILIATION_WU_PAGE_FETCH = "jira_project_scan.page_fetch"
JIRA_PROJECT_RECONCILIATION_WU_ISSUE_DETAIL_FETCH = (
    "jira_project_scan.issue_detail_fetch"
)
JIRA_PROJECT_RECONCILIATION_WU_CLASSIFICATION_COMPUTE = (
    "jira_issue_classification.compute"
)
JIRA_PROJECT_RECONCILIATION_WU_LABEL_REPLACE = "jira_label_reconciliation.label_replace"
JIRA_PROJECT_RECONCILIATION_WU_PARENT_UPSERT = (
    "parent_workflow_reconciliation.parent_upsert"
)
JIRA_PROJECT_RECONCILIATION_WU_SUMMARY_COMPUTE = "reconciliation_summary.compute"

_EXTERNAL_RETRY = WorkflowWorkUnitRetryPolicy(
    max_attempts=3,
    initial_interval_seconds=30,
    max_interval_seconds=300,
    backoff_coefficient=2.0,
)


class JiraProjectReconciliationWorkflow:
    @workflow_work_unit(
        key=JIRA_PROJECT_RECONCILIATION_WU_PAGE_FETCH,
        step_key=JIRA_PROJECT_RECONCILIATION_STEP_SCAN,
        label="Fetch Jira issue page",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=_EXTERNAL_RETRY,
    )
    @workflow_work_unit(
        key=JIRA_PROJECT_RECONCILIATION_WU_ISSUE_DETAIL_FETCH,
        step_key=JIRA_PROJECT_RECONCILIATION_STEP_SCAN,
        label="Fetch Jira issue detail",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=_EXTERNAL_RETRY,
    )
    @workflow_step(
        key=JIRA_PROJECT_RECONCILIATION_STEP_SCAN,
        label="Jira project scan",
        kind=WorkflowStepKind.INTEGRATION,
        retryable=True,
        description="Scan the Jira project and load issue details.",
    )
    def jira_project_scan(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key=JIRA_PROJECT_RECONCILIATION_WU_CLASSIFICATION_COMPUTE,
        step_key=JIRA_PROJECT_RECONCILIATION_STEP_CLASSIFICATION,
        label="Classify Jira issues",
        kind=WorkflowWorkUnitKind.PURE_COMPUTE,
        idempotency_policy=WorkflowWorkUnitIdempotencyPolicy(required=True),
    )
    @workflow_step(
        key=JIRA_PROJECT_RECONCILIATION_STEP_CLASSIFICATION,
        label="Jira issue classification",
        kind=WorkflowStepKind.BUSINESS,
        after=JIRA_PROJECT_RECONCILIATION_STEP_SCAN,
        retryable=True,
        description="Classify Jira issues using hierarchy metadata and compute MB-owned labels.",
    )
    def jira_issue_classification(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key=JIRA_PROJECT_RECONCILIATION_WU_LABEL_REPLACE,
        step_key=JIRA_PROJECT_RECONCILIATION_STEP_LABEL_RECONCILIATION,
        label="Replace Jira labels",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=_EXTERNAL_RETRY,
    )
    @workflow_step(
        key=JIRA_PROJECT_RECONCILIATION_STEP_LABEL_RECONCILIATION,
        label="Jira label reconciliation",
        kind=WorkflowStepKind.SIDE_EFFECT,
        after=JIRA_PROJECT_RECONCILIATION_STEP_CLASSIFICATION,
        retryable=True,
        description="Apply MB-owned labels without touching customer labels.",
    )
    def jira_label_reconciliation(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key=JIRA_PROJECT_RECONCILIATION_WU_PARENT_UPSERT,
        step_key=JIRA_PROJECT_RECONCILIATION_STEP_PARENT_RECONCILIATION,
        label="Upsert parent planning workflow",
        kind=WorkflowWorkUnitKind.SIDE_EFFECT,
        retry_policy=_EXTERNAL_RETRY,
    )
    @workflow_step(
        key=JIRA_PROJECT_RECONCILIATION_STEP_PARENT_RECONCILIATION,
        label="Parent workflow reconciliation",
        kind=WorkflowStepKind.BUSINESS,
        after=JIRA_PROJECT_RECONCILIATION_STEP_LABEL_RECONCILIATION,
        retryable=True,
        description="Match or queue parent planning workflows by stable Jira issue id.",
    )
    def parent_workflow_reconciliation(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key=JIRA_PROJECT_RECONCILIATION_WU_SUMMARY_COMPUTE,
        step_key=JIRA_PROJECT_RECONCILIATION_STEP_SUMMARY,
        label="Build reconciliation summary",
        kind=WorkflowWorkUnitKind.PURE_COMPUTE,
        idempotency_policy=WorkflowWorkUnitIdempotencyPolicy(required=True),
    )
    @workflow_step(
        key=JIRA_PROJECT_RECONCILIATION_STEP_SUMMARY,
        label="Reconciliation summary",
        kind=WorkflowStepKind.BUSINESS,
        after=JIRA_PROJECT_RECONCILIATION_STEP_PARENT_RECONCILIATION,
        retryable=True,
        description="Persist the reconciliation summary for this project execution.",
    )
    def reconciliation_summary(self) -> None:
        raise NotImplementedError
