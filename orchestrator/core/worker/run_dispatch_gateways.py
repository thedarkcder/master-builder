from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


GatewayFn = Callable[..., Any]


def _require_callable(name: str, value: GatewayFn | None) -> GatewayFn:
    if value is None or not callable(value):
        raise TypeError(f"Run dispatch gateway requires callable {name}")
    return value


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
    cleanup_run_workspaces_fn: GatewayFn
    build_run_heartbeat_controller_fn: GatewayFn
    emit_agent_event_fn: GatewayFn
    resolve_agent_id_fn: GatewayFn
    resolve_worker_service_instance_id_fn: GatewayFn

    def __post_init__(self) -> None:
        _require_callable("cleanup_run_workspaces_fn", self.cleanup_run_workspaces_fn)
        _require_callable("build_run_heartbeat_controller_fn", self.build_run_heartbeat_controller_fn)
        _require_callable("emit_agent_event_fn", self.emit_agent_event_fn)
        _require_callable("resolve_agent_id_fn", self.resolve_agent_id_fn)
        _require_callable("resolve_worker_service_instance_id_fn", self.resolve_worker_service_instance_id_fn)


@dataclass(frozen=True)
class RunProjectGateway:
    resolve_project_for_run_fn: GatewayFn
    fail_missing_project_mapping_fn: GatewayFn
    block_archived_project_fn: GatewayFn
    bind_run_project_fn: GatewayFn
    tenant_jira_issue_url_fn: GatewayFn
    transition_issue_status_fn: GatewayFn | None = None

    def __post_init__(self) -> None:
        _require_callable("resolve_project_for_run_fn", self.resolve_project_for_run_fn)
        _require_callable("fail_missing_project_mapping_fn", self.fail_missing_project_mapping_fn)
        _require_callable("block_archived_project_fn", self.block_archived_project_fn)
        _require_callable("bind_run_project_fn", self.bind_run_project_fn)
        _require_callable("tenant_jira_issue_url_fn", self.tenant_jira_issue_url_fn)
        if self.transition_issue_status_fn is not None:
            _require_callable("transition_issue_status_fn", self.transition_issue_status_fn)


@dataclass(frozen=True)
class RunStageUpdateGateway:
    send_discord_message_fn: GatewayFn
    send_jira_message_fn: GatewayFn
    lock_acquired_update_fn: GatewayFn
    repo_setup_ready_update_fn: GatewayFn | None = None
    plan_posted_update_fn: GatewayFn | None = None
    pr_opened_update_fn: GatewayFn | None = None
    run_failed_update_fn: GatewayFn | None = None
    run_requeued_repo_setup_update_fn: GatewayFn | None = None
    run_requeued_capability_update_fn: GatewayFn | None = None
    run_requeued_stale_snapshot_update_fn: GatewayFn | None = None

    def __post_init__(self) -> None:
        _require_callable("send_discord_message_fn", self.send_discord_message_fn)
        _require_callable("send_jira_message_fn", self.send_jira_message_fn)
        _require_callable("lock_acquired_update_fn", self.lock_acquired_update_fn)


@dataclass(frozen=True)
class RunExecutionGateway:
    promote_run_to_running_fn: GatewayFn
    workflow_request_for_run_fn: GatewayFn
    fail_guardrail_violation_fn: GatewayFn
    ensure_project_repository_checkout_fn: GatewayFn
    fail_project_repository_checkout_fn: GatewayFn
    fail_project_repository_setup_fn: GatewayFn
    finalize_cancelled_run_fn: GatewayFn
    finalize_workflow_result_fn: GatewayFn
    persist_stage_checkpoint_fn: GatewayFn
    requeue_run_for_repo_setup_fn: GatewayFn
    requeue_workflow_result_for_capability_fn: GatewayFn
    requeue_workflow_result_for_stale_snapshot_fn: GatewayFn
    check_run_snapshot_freshness_fn: GatewayFn

    def __post_init__(self) -> None:
        for name in (
            "promote_run_to_running_fn",
            "workflow_request_for_run_fn",
            "fail_guardrail_violation_fn",
            "ensure_project_repository_checkout_fn",
            "fail_project_repository_checkout_fn",
            "fail_project_repository_setup_fn",
            "finalize_cancelled_run_fn",
            "finalize_workflow_result_fn",
            "persist_stage_checkpoint_fn",
            "requeue_run_for_repo_setup_fn",
            "requeue_workflow_result_for_capability_fn",
            "requeue_workflow_result_for_stale_snapshot_fn",
            "check_run_snapshot_freshness_fn",
        ):
            _require_callable(name, getattr(self, name))
