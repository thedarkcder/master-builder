from __future__ import annotations

from sqlalchemy import desc, or_, select

from orchestrator.api.admin.workflows.execution_state_read_model import build_workflow_execution_state_read_model
from orchestrator.api.admin.workflows.queries import (
    active_followup_contexts,
    audit_events_by_operation,
    pending_input_request,
    workflow_checkpoint_kinds,
    workflow_operation_attempts_by_operation,
    workflow_operations,
    workflow_runs,
)
from orchestrator.api.admin.workflows.type_read_model import workflow_operation_reads
from orchestrator.api.schemas import (
    WorkflowBoardChildIssueRead,
    WorkflowBoardItemRead,
    WorkflowBoardRunSummaryRead,
    WorkflowLinkRead,
)
from orchestrator.core.integrations.atlassian.links import tenant_jira_issue_url
from orchestrator.storage.models import (
    Run,
    RunHumanInputRequest,
    Tenant,
    WorkflowCheckpoint,
    WorkflowExecutableWorkItem,
    WorkflowExecution,
)


BOARD_EXCLUDED_WORKFLOW_STATUSES = ("cancelled",)
BOARD_ACTIVE_RUN_STATUSES = frozenset(
    ("queued", "running", "processing", "retrying", "review", "in_review", "blocked", "failed")
)
STARTABLE_CHILD_WORK_STATES = frozenset({"planning_candidate"})
STARTED_CHILD_RUN_STATUSES = frozenset(
    ("queued", "dispatching", "running", "waiting_for_input", "blocked", "failed", "succeeded", "cancelled")
)


def _run_activity_at(run: Run):  # noqa: ANN001
    return run.finished_at or run.started_at or run.created_at


def _workflow_activity_at(workflow: WorkflowExecution, runs: list[Run]):  # noqa: ANN001
    candidates = [
        workflow.updated_at,
        workflow.finished_at,
        workflow.started_at,
        workflow.created_at,
        *[_run_activity_at(run) for run in runs],
    ]
    return max(candidate for candidate in candidates if candidate is not None)


def _run_summary(run: Run) -> WorkflowBoardRunSummaryRead:
    return WorkflowBoardRunSummaryRead(
        run_id=run.run_id,
        workflow_id=run.workflow_id,
        issue_key=run.issue_key,
        issue_summary=run.issue_summary,
        status=run.status,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _latest_child_runs_by_issue(*, session, tenant_id: str, project_id: str | None) -> dict[str, Run]:  # noqa: ANN001
    query = (
        select(Run)
        .where(
            Run.tenant_id == tenant_id,
            Run.dedupe_scope == "issue_execution",
            Run.status.in_(STARTED_CHILD_RUN_STATUSES),
        )
        .order_by(Run.issue_key.asc(), desc(Run.created_at))
    )
    if project_id:
        query = query.where(Run.project_id == project_id)
    latest: dict[str, Run] = {}
    for run in session.execute(query).scalars():
        issue_key = str(run.issue_key or "").strip().upper()
        if issue_key:
            latest.setdefault(issue_key, run)
    return latest


def _board_children_by_workflow(
    *,
    session,
    parent_workflow_ids: list[str],
) -> dict[str, list[WorkflowExecutableWorkItem]]:  # noqa: ANN001
    if not parent_workflow_ids:
        return {}
    children_by_workflow: dict[str, list[WorkflowExecutableWorkItem]] = {workflow_id: [] for workflow_id in parent_workflow_ids}
    rows = session.execute(
        select(WorkflowExecutableWorkItem)
        .where(
            WorkflowExecutableWorkItem.parent_workflow_id.in_(parent_workflow_ids),
            WorkflowExecutableWorkItem.item_kind == "child",
        )
        .order_by(WorkflowExecutableWorkItem.issue_key.asc())
    ).scalars()
    for row in rows:
        children_by_workflow.setdefault(row.parent_workflow_id, []).append(row)
    return children_by_workflow


def _child_start_blocked_reason(*, run: Run | None, mb_work_state: str | None) -> str | None:
    if run is not None:
        return "already_started"
    if mb_work_state not in STARTABLE_CHILD_WORK_STATES:
        return "not_ready"
    return None


def _child_issue_read(
    *,
    child: WorkflowExecutableWorkItem,
    run: Run | None,
    child_starts_enabled: bool,
) -> WorkflowBoardChildIssueRead:
    base_startable = run is None and child.mb_work_state in STARTABLE_CHILD_WORK_STATES
    startable = base_startable and child_starts_enabled
    start_blocked_reason = _child_start_blocked_reason(run=run, mb_work_state=child.mb_work_state)
    if base_startable and not child_starts_enabled:
        start_blocked_reason = "parent_not_completed"
    return WorkflowBoardChildIssueRead(
        work_item_id=child.work_item_id,
        issue_key=child.issue_key,
        summary=child.issue_summary,
        status=child.issue_status,
        mb_work_state=child.mb_work_state,
        issue_type=child.issue_type,
        run_id=run.run_id if run is not None else None,
        run_status=run.status if run is not None else None,
        startable=startable,
        start_label="Start" if startable else None,
        start_blocked_reason=start_blocked_reason,
    )


def _parent_start_metadata(
    *,
    parent_work_item: WorkflowExecutableWorkItem,
    workflow: WorkflowExecution,
    children: list[WorkflowBoardChildIssueRead],
    pending_request: RunHumanInputRequest | None,
) -> tuple[bool, str | None, str | None]:
    status = str(workflow.status or "").strip().casefold()
    if pending_request is not None or status == "waiting_for_input":
        return False, None, "waiting_for_input"
    if status in {"failed", "blocked"}:
        return False, None, status
    if status in {"queued", "pending"} and str(workflow.workflow_type_key or "").strip() == "parent_planning":
        return True, "Start planning", None
    if status != "completed":
        return False, None, "not_ready"
    if children:
        return False, None, "start_children_individually"
    if parent_work_item.mb_work_state in STARTABLE_CHILD_WORK_STATES:
        return True, "Start run", None
    return False, None, "not_ready"


def _board_links(*, session, tenant: Tenant | None, workflow: WorkflowExecution) -> list[WorkflowLinkRead]:  # noqa: ANN001
    workflow_source_ref = str(workflow.source_ref or "").strip()
    is_jira_workflow = str(workflow.source_system or "").strip() == "jira"
    jira_url = (
        tenant_jira_issue_url(session=session, tenant=tenant, issue_key=workflow_source_ref)
        if tenant is not None and is_jira_workflow
        else None
    )
    if not jira_url:
        return []
    return [
        WorkflowLinkRead(
            kind="jira_issue",
            label=f"Jira issue {workflow_source_ref}",
            ref=workflow_source_ref,
            url=jira_url,
            status=workflow.status,
        )
    ]


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
        runs=[run_to_schema_fn(run, workflow_execution_id=workflow.execution_id) for run in runs],
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


def list_workflow_board_items(
    *,
    session,
    tenant_id: str,
    project_id: str | None,
    limit: int,
    offset: int,
) -> list[WorkflowBoardItemRead]:  # noqa: ANN001
    query = (
        select(WorkflowExecutableWorkItem, WorkflowExecution)
        .join(WorkflowExecution, WorkflowExecution.workflow_id == WorkflowExecutableWorkItem.parent_workflow_id)
        .where(
            WorkflowExecutableWorkItem.tenant_id == tenant_id,
            WorkflowExecutableWorkItem.item_kind == "parent",
            WorkflowExecution.status.notin_(BOARD_EXCLUDED_WORKFLOW_STATUSES),
        )
    )
    if project_id:
        query = query.where(WorkflowExecutableWorkItem.project_id == project_id)
    parent_rows = (
        session.execute(
            query.order_by(desc(WorkflowExecution.updated_at), desc(WorkflowExecutableWorkItem.updated_at))
            .limit(limit)
            .offset(offset)
        )
        .all()
    )
    workflows = [workflow for _parent_work_item, workflow in parent_rows]
    workflow_ids = [workflow.workflow_id for workflow in workflows]
    if not workflow_ids:
        return []

    tenant = session.get(Tenant, tenant_id)
    children_by_workflow = _board_children_by_workflow(session=session, parent_workflow_ids=workflow_ids)
    latest_runs_by_issue = _latest_child_runs_by_issue(session=session, tenant_id=tenant_id, project_id=project_id)

    pending_request_by_workflow: dict[str, RunHumanInputRequest] = {}
    pending_requests = session.execute(
        select(RunHumanInputRequest)
        .where(
            RunHumanInputRequest.workflow_id.in_(workflow_ids),
            RunHumanInputRequest.status == "pending",
        )
        .order_by(desc(RunHumanInputRequest.created_at))
    ).scalars()
    for request in pending_requests:
        pending_request_by_workflow.setdefault(request.workflow_id, request)

    runs_by_workflow: dict[str, list[Run]] = {workflow_id: [] for workflow_id in workflow_ids}
    run_rows = session.execute(
        select(Run)
        .where(Run.workflow_id.in_(workflow_ids))
        .order_by(Run.workflow_id.asc(), desc(Run.attempt_number))
    ).scalars()
    for run in run_rows:
        runs_by_workflow.setdefault(run.workflow_id, []).append(run)

    board_items: list[WorkflowBoardItemRead] = []
    for parent_work_item, workflow in parent_rows:
        workflow_runs = runs_by_workflow.get(workflow.workflow_id, [])
        sorted_runs = sorted(workflow_runs, key=_run_activity_at, reverse=True)
        latest_run = sorted_runs[0] if sorted_runs else None
        run_summaries = [_run_summary(run) for run in sorted_runs]
        pending_request = pending_request_by_workflow.get(workflow.workflow_id)
        workflow_status = str(workflow.status or "").strip().casefold()
        children = [
            _child_issue_read(
                child=child,
                run=latest_runs_by_issue.get(str(child.issue_key or "").strip().upper()),
                child_starts_enabled=workflow_status == "completed",
            )
            for child in children_by_workflow.get(workflow.workflow_id, [])
        ]
        startable, start_label, start_blocked_reason = _parent_start_metadata(
            parent_work_item=parent_work_item,
            workflow=workflow,
            children=children,
            pending_request=pending_request,
        )
        board_items.append(
            WorkflowBoardItemRead(
                work_item_id=parent_work_item.work_item_id,
                execution_id=workflow.execution_id,
                workflow_id=workflow.workflow_id,
                workflow_type_key=workflow.workflow_type_key,
                tenant_id=workflow.tenant_id,
                project_id=workflow.project_id,
                source_system=workflow.source_system,
                source_ref=workflow.source_ref,
                display_name=parent_work_item.issue_summary or workflow.display_name,
                dedupe_scope=workflow.dedupe_scope,
                status=workflow.status,
                failure_reason=workflow.last_error,
                pending_input_request_id=pending_request.request_id if pending_request is not None else None,
                startable=startable,
                start_label=start_label,
                start_blocked_reason=start_blocked_reason,
                run_count=len(sorted_runs),
                active_run_count=sum(1 for run in sorted_runs if str(run.status).lower() in BOARD_ACTIVE_RUN_STATUSES),
                failed_run_count=sum(1 for run in sorted_runs if str(run.status).lower() == "failed"),
                latest_run=_run_summary(latest_run) if latest_run is not None else None,
                runs=run_summaries,
                children=children,
                links=_board_links(session=session, tenant=tenant, workflow=workflow),
                latest_activity_at=_workflow_activity_at(workflow, sorted_runs),
                created_at=workflow.created_at,
                started_at=workflow.started_at,
                finished_at=workflow.finished_at,
                updated_at=workflow.updated_at,
            )
        )
    return sorted(board_items, key=lambda item: item.latest_activity_at, reverse=True)
