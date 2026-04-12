from __future__ import annotations

from datetime import datetime, timezone

from orchestrator.storage.models import Run, RunHumanInputRequest, WorkflowCheckpoint, WorkflowExecution


def make_run(
    *,
    run_id: str,
    tenant_id: str,
    issue_key: str,
    issue_summary: str,
    created_at: datetime,
    workflow_id: str | None = None,
    project_id: str | None = None,
    issue_description: str | None = None,
    repo_url: str | None = None,
    branch: str | None = None,
    pr_url: str | None = None,
    attempt_number: int = 1,
    parent_run_id: str | None = None,
    entry_mode: str = "fresh",
    entry_stage: str | None = "orchestrated",
    entry_checkpoint_id: str | None = None,
    dedupe_scope: str = "issue_execution",
    status: str = "queued",
    last_error: str | None = None,
    pre_check_outcome: str | None = None,
    required_worker_capability: str | None = None,
    claim_id: str | None = None,
    plan: dict | None = None,
    dispatch_claimed_at: datetime | None = None,
    started_at: datetime | None = None,
    last_heartbeat_at: datetime | None = None,
    worker_service_instance_id: str | None = None,
    finished_at: datetime | None = None,
) -> Run:
    return Run(
        run_id=run_id,
        workflow_id=workflow_id or f"workflow-{run_id}",
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        repo_url=repo_url,
        branch=branch,
        pr_url=pr_url,
        attempt_number=attempt_number,
        parent_run_id=parent_run_id,
        entry_mode=entry_mode,
        entry_stage=entry_stage,
        entry_checkpoint_id=entry_checkpoint_id,
        dedupe_scope=dedupe_scope,
        status=status,
        last_error=last_error,
        pre_check_outcome=pre_check_outcome,
        required_worker_capability=required_worker_capability,
        claim_id=claim_id,
        plan=plan,
        created_at=created_at,
        dispatch_claimed_at=dispatch_claimed_at,
        started_at=started_at,
        last_heartbeat_at=last_heartbeat_at,
        worker_service_instance_id=worker_service_instance_id,
        finished_at=finished_at,
    )


def add_run_with_workflow(
    session,
    run: Run,
    *,
    workflow_status: str | None = None,
    latest_checkpoint: WorkflowCheckpoint | None = None,
    source_workflow_id: str | None = None,
    source_run_id: str | None = None,
    blocked_reason: str | None = None,
) -> WorkflowExecution:
    if not run.workflow_id:
        run.workflow_id = f"workflow-{run.run_id}"
    if not run.attempt_number:
        run.attempt_number = 1
    if not run.entry_mode:
        run.entry_mode = "fresh"
    if run.entry_stage is None:
        run.entry_stage = "orchestrated"
    timestamp = run.created_at or datetime.now(timezone.utc)
    effective_workflow_status = workflow_status or run.status
    active_run_id = run.run_id if effective_workflow_status in {"queued", "dispatching", "running", "waiting_for_input"} else None
    workflow = WorkflowExecution(
        workflow_id=run.workflow_id,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        issue_key=run.issue_key,
        issue_summary=run.issue_summary,
        issue_description=run.issue_description,
        repo_url=run.repo_url,
        branch=run.branch,
        pr_url=run.pr_url,
        dedupe_scope=run.dedupe_scope,
        status=effective_workflow_status,
        last_error=run.last_error,
        active_run_id=active_run_id,
        latest_checkpoint_id=latest_checkpoint.checkpoint_id if latest_checkpoint is not None else None,
        source_workflow_id=source_workflow_id,
        source_run_id=source_run_id,
        blocked_reason=blocked_reason if blocked_reason is not None else (run.last_error if effective_workflow_status == "blocked" else None),
        created_at=timestamp,
        started_at=run.started_at,
        finished_at=run.finished_at if effective_workflow_status in {"succeeded", "failed", "cancelled"} else None,
        updated_at=run.finished_at or run.started_at or timestamp,
    )
    session.add(workflow)
    session.add(run)
    if latest_checkpoint is not None:
        session.add(latest_checkpoint)
    return workflow


def add_workflow_attempt(
    session,
    *,
    workflow_id: str | None = None,
    run_id: str,
    tenant_id: str,
    project_id: str | None,
    issue_key: str,
    issue_summary: str,
    issue_description: str | None = None,
    repo_url: str | None = None,
    branch: str | None = None,
    pr_url: str | None = None,
    dedupe_scope: str = "issue_execution",
    workflow_status: str = "queued",
    run_status: str = "queued",
    attempt_number: int = 1,
    parent_run_id: str | None = None,
    entry_mode: str = "fresh",
    entry_stage: str | None = "orchestrated",
    entry_checkpoint_id: str | None = None,
    checkpoint_kind: str | None = None,
    checkpoint_stage: str | None = None,
    checkpoint_payload: dict | None = None,
    checkpoint_session_id: str | None = None,
    blocked_reason: str | None = None,
    last_error: str | None = None,
    pre_check_outcome: str | None = None,
    required_worker_capability: str | None = None,
    claim_id: str | None = None,
    plan: dict | None = None,
    dispatch_claimed_at: datetime | None = None,
    worker_service_instance_id: str | None = None,
    source_workflow_id: str | None = None,
    source_run_id: str | None = None,
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    last_heartbeat_at: datetime | None = None,
    updated_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[WorkflowExecution, Run, WorkflowCheckpoint | None]:
    timestamp = now or created_at or datetime.now(timezone.utc)
    normalized_workflow_id = workflow_id or f"workflow-{run_id}"
    workflow_started = started_at if started_at is not None else (timestamp if workflow_status not in {"queued", "dispatching"} else None)
    workflow_finished = finished_at if finished_at is not None else (
        timestamp if workflow_status in {"succeeded", "failed", "cancelled"} else None
    )
    run_started = started_at if started_at is not None else (timestamp if run_status not in {"queued", "dispatching"} else None)
    run_finished = finished_at if finished_at is not None else (
        timestamp if run_status in {"succeeded", "failed", "cancelled", "blocked"} else None
    )
    workflow = WorkflowExecution(
        workflow_id=normalized_workflow_id,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        repo_url=repo_url,
        branch=branch,
        pr_url=pr_url,
        dedupe_scope=dedupe_scope,
        status=workflow_status,
        last_error=last_error,
        active_run_id=run_id if workflow_status in {"queued", "dispatching", "running", "waiting_for_input", "blocked"} else None,
        latest_checkpoint_id=entry_checkpoint_id,
        source_workflow_id=source_workflow_id,
        source_run_id=source_run_id,
        blocked_reason=blocked_reason,
        created_at=created_at or timestamp,
        started_at=workflow_started,
        finished_at=workflow_finished,
        updated_at=updated_at or timestamp,
    )
    run = Run(
        run_id=run_id,
        workflow_id=normalized_workflow_id,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        repo_url=repo_url,
        branch=branch,
        pr_url=pr_url,
        attempt_number=attempt_number,
        parent_run_id=parent_run_id,
        entry_mode=entry_mode,
        entry_stage=entry_stage,
        entry_checkpoint_id=entry_checkpoint_id,
        dedupe_scope=dedupe_scope,
        status=run_status,
        last_error=last_error,
        pre_check_outcome=pre_check_outcome,
        required_worker_capability=required_worker_capability,
        claim_id=claim_id,
        plan=plan,
        created_at=created_at or timestamp,
        dispatch_claimed_at=dispatch_claimed_at,
        started_at=run_started,
        last_heartbeat_at=last_heartbeat_at if last_heartbeat_at is not None else (timestamp if run_status == "running" else None),
        worker_service_instance_id=worker_service_instance_id,
        finished_at=run_finished,
    )
    checkpoint = None
    if entry_checkpoint_id and checkpoint_kind:
        checkpoint = WorkflowCheckpoint(
            checkpoint_id=entry_checkpoint_id,
            workflow_id=normalized_workflow_id,
            run_id=run_id,
            checkpoint_kind=checkpoint_kind,
            stage=checkpoint_stage or entry_stage,
            payload_json=dict(checkpoint_payload or {}),
            codex_session_id=checkpoint_session_id,
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(checkpoint)
    session.add(workflow)
    session.add(run)
    return workflow, run, checkpoint


def add_human_input_request(
    session,
    *,
    request_id: str,
    tenant_id: str,
    project_id: str | None,
    workflow_id: str,
    checkpoint_id: str,
    source_run_id: str,
    issue_key: str,
    source_stage: str,
    request_type: str,
    status: str,
    prompt: str = "Need more input",
    instructions: str | None = None,
    expected_reply_format: str | None = None,
    consumed_by_run_id: str | None = None,
    thread_channel_id: str | None = None,
    thread_message_id: str | None = None,
    answer_encrypted: bytes | None = None,
    answer_source_ref: str | None = None,
    answered_at: datetime | None = None,
    expires_at: datetime | None = None,
    request_context_json: dict | None = None,
    now: datetime | None = None,
) -> RunHumanInputRequest:
    timestamp = now or datetime.now(timezone.utc)
    request = RunHumanInputRequest(
        request_id=request_id,
        tenant_id=tenant_id,
        project_id=project_id,
        workflow_id=workflow_id,
        checkpoint_id=checkpoint_id,
        source_run_id=source_run_id,
        consumed_by_run_id=consumed_by_run_id,
        issue_key=issue_key,
        source_stage=source_stage,
        request_type=request_type,
        prompt=prompt,
        instructions=instructions,
        expected_reply_format=expected_reply_format,
        status=status,
        request_context_json=dict(request_context_json or {}),
        thread_channel_id=thread_channel_id,
        thread_message_id=thread_message_id,
        answer_encrypted=answer_encrypted,
        answer_source_ref=answer_source_ref,
        answered_at=answered_at,
        expires_at=expires_at,
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(request)
    return request
