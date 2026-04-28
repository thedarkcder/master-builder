from __future__ import annotations

from sqlalchemy import desc, or_, select

from orchestrator.api.admin.workflow_execution_state_read_model import build_workflow_execution_state_read_model
from orchestrator.api.admin.workflow_queries import (
    active_followup_contexts,
    audit_events_by_operation,
    pending_input_request,
    workflow_checkpoint_kinds,
    workflow_operation_attempts_by_operation,
    workflow_operations,
    workflow_runs,
)
from orchestrator.api.admin.workflow_type_read_model import workflow_operation_reads
from orchestrator.api.schemas import WorkflowLinkRead
from orchestrator.core.jira_links import tenant_jira_issue_url
from orchestrator.storage.models import Tenant, WorkflowCheckpoint, WorkflowExecution


def workflow_links(
    *,
    session,
    workflow: WorkflowExecution,
    tenant: Tenant | None,
    followup_contexts: list,
    workflow_runs: list,
) -> list[WorkflowLinkRead]:  # noqa: ANN001
    links: list[WorkflowLinkRead] = []
    workflow_source_ref = str(workflow.source_ref or "").strip()
    is_jira_workflow = str(workflow.source_system or "").strip() == "jira"
    jira_url = (
        tenant_jira_issue_url(session=session, tenant=tenant, issue_key=workflow_source_ref)
        if tenant is not None and is_jira_workflow
        else None
    )
    if jira_url:
        links.append(
            WorkflowLinkRead(
                kind="jira_issue",
                label=f"Jira issue {workflow_source_ref}",
                ref=workflow_source_ref,
                url=jira_url,
                status=workflow.status,
            )
        )
    if workflow.pr_url:
        links.append(
            WorkflowLinkRead(
                kind="pull_request",
                label="Pull request",
                ref=workflow.pr_url,
                url=workflow.pr_url,
            )
        )
    for run in workflow_runs:
        links.append(
            WorkflowLinkRead(
                kind="run",
                label=f"Run {run.run_id}",
                ref=run.run_id,
                url=None,
                status=run.status,
            )
        )
    for followup in followup_contexts:
        channel_ref = str(followup.thread_channel_id or followup.root_message_id or "").strip()
        if not channel_ref:
            continue
        links.append(
            WorkflowLinkRead(
                kind="followup",
                label="Follow-up thread",
                ref=channel_ref,
                status=followup.status,
            )
        )
    return links


def workflow_schema(
    *,
    session,
    workflow,
    workflow_to_schema_fn,
    run_to_schema_fn,
):  # noqa: ANN001
    latest_checkpoint = session.get(WorkflowCheckpoint, workflow.latest_checkpoint_id) if workflow.latest_checkpoint_id else None
    checkpoint_kinds = workflow_checkpoint_kinds(session=session, workflow_id=workflow.workflow_id)
    runs = workflow_runs(session=session, workflow_id=workflow.workflow_id)
    pending_request = pending_input_request(session=session, workflow_id=workflow.workflow_id)
    operations = workflow_operations(session=session, workflow_id=workflow.workflow_id)
    operation_attempts = workflow_operation_attempts_by_operation(session=session, workflow_id=workflow.workflow_id)
    operation_events = audit_events_by_operation(session=session, workflow_id=workflow.workflow_id)
    workflow_type, operation_reads = workflow_operation_reads(
        session=session,
        workflow=workflow,
        operations=operations,
        operation_attempts=operation_attempts,
        operation_events=operation_events,
    )
    workflow_state = build_workflow_execution_state_read_model(
        workflow=workflow,
        workflow_type=workflow_type,
        latest_run=runs[-1] if runs else None,
        operations=operations,
        pending_request=pending_request,
        checkpoint_kinds=checkpoint_kinds,
        runs=runs,
    )
    tenant = session.get(Tenant, workflow.tenant_id)
    followup_contexts = (
        active_followup_contexts(session=session, tenant_id=workflow.tenant_id, issue_key=workflow.source_ref)
        if str(workflow.source_system or "").strip() == "jira"
        else []
    )
    return workflow_to_schema_fn(
        workflow,
        runs=[run_to_schema_fn(run) for run in runs],
        pending_input_request_id=(pending_request.request_id if pending_request is not None else None),
        latest_checkpoint_kind=latest_checkpoint.checkpoint_kind if latest_checkpoint is not None else None,
        workflow_type=workflow_type,
        current_state=str(workflow.status or "").strip() or "unknown",
        waiting_on=workflow_state.waiting_on,
        next_step=workflow_state.next_step,
        state_path=workflow_state.state_path,
        completed_steps=workflow_state.step_buckets.completed,
        failed_steps=workflow_state.step_buckets.failed,
        pending_steps=workflow_state.step_buckets.pending,
        retrying_steps=workflow_state.step_buckets.retrying,
        conditional_branches_taken=workflow_state.conditional_branches_taken,
        conditional_branches_available=workflow_state.conditional_branches_available,
        can_resume=workflow_state.can_resume,
        resume_unavailable_reason=workflow_state.resume_unavailable_reason,
        links=workflow_links(
            session=session,
            workflow=workflow,
            tenant=tenant,
            followup_contexts=followup_contexts,
            workflow_runs=runs,
    ),
        operations=operation_reads,
    )


def list_workflow_schemas(
    *,
    session,
    tenant_id: str | None,
    project_id: str | None,
    status_filter: str | None,
    issue_query: str | None,
    limit: int,
    offset: int,
    workflow_to_schema_fn,
    run_to_schema_fn,
):  # noqa: ANN001
    query = select(WorkflowExecution)
    if tenant_id:
        query = query.where(WorkflowExecution.tenant_id == tenant_id)
    if project_id:
        query = query.where(WorkflowExecution.project_id == project_id)
    if status_filter:
        query = query.where(WorkflowExecution.status == status_filter)
    normalized_issue = str(issue_query or "").strip()
    if normalized_issue:
        like_value = f"%{normalized_issue}%"
        query = query.where(
            or_(
                WorkflowExecution.source_ref.ilike(like_value),
                WorkflowExecution.display_name.ilike(like_value),
            )
        )
    query = query.order_by(desc(WorkflowExecution.created_at)).limit(limit).offset(offset)
    return [
        workflow_schema(
            session=session,
            workflow=workflow,
            workflow_to_schema_fn=workflow_to_schema_fn,
            run_to_schema_fn=run_to_schema_fn,
        )
        for workflow in session.execute(query).scalars().all()
    ]
