from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunDispatchStatusConfig:
    queued: str = "queued"
    running: str = "running"
    failed: str = "failed"
    blocked: str = "blocked"
    cancelled: str = "cancelled"
    dispatching: str = "dispatching"


@dataclass(frozen=True)
class RunDispatchIdentityGateway:
    logger: object
    cleanup_run_workspaces_fn: object
    build_run_heartbeat_controller_fn: object
    emit_agent_event_fn: object
    resolve_agent_id_fn: object
    resolve_worker_service_instance_id_fn: object


@dataclass(frozen=True)
class RunProjectGateway:
    resolve_project_for_run_fn: object
    fail_missing_project_mapping_fn: object
    block_archived_project_fn: object
    bind_run_project_fn: object
    tenant_jira_issue_url_fn: object
    transition_issue_status_fn: object | None = None


@dataclass(frozen=True)
class RunStageUpdateGateway:
    send_discord_message_fn: object
    send_jira_message_fn: object
    lock_acquired_update_fn: object
    repo_setup_ready_update_fn: object | None = None
    plan_posted_update_fn: object | None = None
    pr_opened_update_fn: object | None = None
    run_failed_update_fn: object | None = None
    run_requeued_repo_setup_update_fn: object | None = None
    run_requeued_capability_update_fn: object | None = None
    run_requeued_stale_snapshot_update_fn: object | None = None


@dataclass(frozen=True)
class RunExecutionGateway:
    promote_run_to_running_fn: object | None
    workflow_request_for_run_fn: object
    fail_guardrail_violation_fn: object
    ensure_project_repository_checkout_fn: object | None = None
    fail_project_repository_checkout_fn: object | None = None
    fail_project_repository_setup_fn: object | None = None
    finalize_cancelled_run_fn: object | None = None
    finalize_workflow_result_fn: object | None = None
    persist_stage_checkpoint_fn: object | None = None
    requeue_run_for_repo_setup_fn: object | None = None
    requeue_workflow_result_for_capability_fn: object | None = None
    requeue_workflow_result_for_stale_snapshot_fn: object | None = None
    check_run_snapshot_freshness_fn: object | None = None
