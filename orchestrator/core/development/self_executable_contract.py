from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from orchestrator.core.parent_feature_workflow.issue_types import (
    is_self_executable_parent_issue_type,
)
from orchestrator.core.parent_feature_workflow.operations import (
    PARENT_WU_BACKLOG_PACKAGE_ASSEMBLY,
)
from orchestrator.core.parent_feature_workflow.self_executable_issue_contract import (
    SelfExecutableIssueContract,
    build_self_executable_issue_contract,
)
from orchestrator.core.projects.parent_feature_brief_store import (
    resolve_parent_feature_brief,
)
from orchestrator.core.workflow.execution_projection import (
    resolve_latest_workflow_execution_by_source,
)
from orchestrator.storage.models import (
    WorkflowExecution,
    WorkflowOperation,
    WorkflowOperationWorkUnit,
)

_BLOCKING_SYNC_LABELS = {"sync-blocked", "sync-stale"}


def _labels(issue) -> set[str]:  # noqa: ANN001
    return {
        str(label or "").strip().casefold()
        for label in getattr(issue, "labels", [])
        if str(label or "").strip()
    }


def _completed_planning_package(
    *, session: Session, workflow_id: str
) -> dict[str, object] | None:
    row = session.execute(
        select(WorkflowOperationWorkUnit)
        .join(
            WorkflowOperation,
            WorkflowOperation.operation_id == WorkflowOperationWorkUnit.operation_id,
        )
        .where(
            WorkflowOperation.workflow_id == workflow_id,
            WorkflowOperationWorkUnit.unit_key == PARENT_WU_BACKLOG_PACKAGE_ASSEMBLY,
            WorkflowOperationWorkUnit.status == "completed",
        )
        .order_by(
            desc(WorkflowOperationWorkUnit.completed_at),
            desc(WorkflowOperationWorkUnit.updated_at),
        )
        .limit(1)
    ).scalar_one_or_none()
    if row is None or not isinstance(row.output_json, dict):
        return None
    package = row.output_json.get("planning_package")
    if not isinstance(package, dict):
        raise RuntimeError(
            f"Completed planning package work unit for workflow {workflow_id} is missing planning_package"
        )
    return package


def _resolve_parent_workflow(
    *,
    session: Session,
    tenant_id: str,
    issue_key: str,
    source_workflow: WorkflowExecution | None,
) -> WorkflowExecution | None:
    workflow = source_workflow
    if workflow is None:
        workflow = resolve_latest_workflow_execution_by_source(
            session=session,
            tenant_id=tenant_id,
            source_system="jira",
            source_ref=issue_key,
        )
    if workflow is None:
        return None
    if str(workflow.workflow_type_key or "").strip() != "parent_planning":
        return None
    if str(workflow.status or "").strip().casefold() != "completed":
        return None
    return workflow


def resolve_self_executable_planning_contract(
    *,
    session: Session,
    tenant_id: str,
    source_issue,
    source_workflow: WorkflowExecution | None = None,
) -> SelfExecutableIssueContract | None:  # noqa: ANN001
    labels = _labels(source_issue)
    if "pm-parent" not in labels:
        return None
    if labels.intersection(_BLOCKING_SYNC_LABELS):
        return None
    if not is_self_executable_parent_issue_type(
        getattr(source_issue, "issue_type", None)
    ):
        return None

    issue_key = str(getattr(source_issue, "key", "") or "").strip().upper()
    if not issue_key:
        raise RuntimeError(
            "Self-executable planning contract requires a source issue key"
        )
    workflow = _resolve_parent_workflow(
        session=session,
        tenant_id=tenant_id,
        issue_key=issue_key,
        source_workflow=source_workflow,
    )
    if workflow is None:
        return None
    planning_package = _completed_planning_package(
        session=session, workflow_id=workflow.workflow_id
    )
    if planning_package is None:
        return None
    product_brief = resolve_parent_feature_brief(
        session=session,
        tenant_id=tenant_id,
        parent_issue_key=issue_key,
    )
    if product_brief is None:
        return None
    return build_self_executable_issue_contract(
        parent_detail=source_issue,
        product_brief=product_brief.to_payload(),
        planning_package=planning_package,
        parent_revision=workflow.workflow_id,
        planning_state=str(planning_package.get("planning_state") or "").strip()
        or None,
    )
