from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from orchestrator.api.jira_oauth.connection_service import tenant_jira_oauth_context
from orchestrator.core.config import get_settings
from orchestrator.core.agent_observability import record_agent_lifecycle_event
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.jira_links import tenant_jira_issue_url
from orchestrator.core.pre_run_check import evaluate_pre_run_check
from orchestrator.core.worker.decision_gate import apply_decision_gate
from orchestrator.core.worker.jira_stage_service import send_stage_update_to_jira as _send_stage_update_to_jira
from orchestrator.core.worker.jira_stage_service import transition_issue_status as _transition_issue_status
from orchestrator.core.worker.queue_selector import select_next_queued_run
from orchestrator.core.worker.run_lifecycle import (
    bind_run_project,
    block_archived_project,
    fail_guardrail_violation,
    fail_missing_project_mapping,
    fail_project_repository_checkout,
    finalize_cancelled_run,
    finalize_workflow_result,
    requeue_workflow_result_for_capability,
    requeue_workflow_result_for_stale_snapshot,
    resolve_project_for_run,
    start_run,
)
from orchestrator.core.worker.stage_events import (
    lock_acquired_update,
    plan_posted_update,
    pr_opened_update,
    run_failed_update,
    run_requeued_capability_mismatch_update,
    run_requeued_stale_snapshot_update,
)
from orchestrator.core.worker.workflow_request_service import (
    build_workflow_request_for_run as _build_workflow_request_for_run,
)
from orchestrator.core.workflow.runner import WorkflowRequest, WorkflowRunner
from orchestrator.api.admin.route_helpers import ensure_project_repository_checkout
from orchestrator.storage.models import Project, Run, Tenant
from orchestrator.tools.project_repo_checkout import check_run_snapshot_freshness
from orchestrator.tools.project_repo_checkout import cleanup_run_workspaces

logger = logging.getLogger(__name__)

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_BLOCKED = "blocked"
RUN_STATUS_CANCELLED = "cancelled"
ASK_REPLY_OPEN_CUSTOM_ID = "ask.reply.open"


def _ask_reply_components() -> list[dict]:
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 2,
                    "label": "Reply",
                    "custom_id": ASK_REPLY_OPEN_CUSTOM_ID,
                }
            ],
        }
    ]


def _workflow_request_for_run(
    session: Session,
    tenant: Tenant,
    run: Run,
    *,
    project: Project | None,
    effective_policy: dict,
) -> WorkflowRequest:
    return _build_workflow_request_for_run(
        session=session,
        tenant=tenant,
        run=run,
        project=project,
        effective_policy=effective_policy,
        settings=get_settings(),
    )


def process_next_queued_run(session: Session, runner: WorkflowRunner) -> Run | None:
    return _process_next_queued_run_impl(session=session, runner=runner, send_discord_message_fn=send_tenant_discord_message)


def _process_next_queued_run_impl(
    *,
    session: Session,
    runner: WorkflowRunner,
    send_discord_message_fn,
) -> Run | None:
    from orchestrator.core.worker.process_service import process_next_queued_run as _process_next_queued_run_impl

    def _apply_decision_gate(
        *,
        session,
        run,
        tenant,
        settings,
        send_discord_message_fn,
        send_jira_message_fn,
        ask_reply_components_fn,
        blocked_status: str,
        failed_status: str,
    ):  # noqa: ANN001
        return apply_decision_gate(
            session=session,
            run=run,
            tenant=tenant,
            settings=settings,
            tenant_jira_oauth_context_fn=tenant_jira_oauth_context,
            evaluate_pre_run_check_fn=evaluate_pre_run_check,
            send_discord_message_fn=send_discord_message_fn,
            send_jira_message_fn=send_jira_message_fn,
            ask_reply_components_fn=ask_reply_components_fn,
            blocked_status=blocked_status,
            failed_status=failed_status,
        )

    def _emit_agent_event(
        *,
        event_type: str,
        tenant_id: str,
        project_id: str | None,
        run_id: str,
        issue_key: str,
        agent_id: str | None,
    ) -> None:
        record_agent_lifecycle_event(
            session=session,
            event_type=event_type,
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=run_id,
            issue_key=issue_key,
            agent_id=agent_id,
        )

    return _process_next_queued_run_impl(
        session=session,
        runner=runner,
        logger=logger,
        settings_fn=get_settings,
        select_next_queued_run_fn=select_next_queued_run,
        apply_decision_gate_fn=_apply_decision_gate,
        send_discord_message_fn=send_discord_message_fn,
        send_jira_message_fn=_send_stage_update_to_jira,
        ask_reply_components_fn=_ask_reply_components,
        resolve_project_for_run_fn=resolve_project_for_run,
        fail_missing_project_mapping_fn=fail_missing_project_mapping,
        block_archived_project_fn=block_archived_project,
        ensure_project_repository_checkout_fn=ensure_project_repository_checkout,
        fail_project_repository_checkout_fn=fail_project_repository_checkout,
        cleanup_run_workspaces_fn=cleanup_run_workspaces,
        start_run_fn=start_run,
        bind_run_project_fn=bind_run_project,
        workflow_request_for_run_fn=_workflow_request_for_run,
        fail_guardrail_violation_fn=fail_guardrail_violation,
        tenant_jira_issue_url_fn=tenant_jira_issue_url,
        lock_acquired_update_fn=lock_acquired_update,
        plan_posted_update_fn=plan_posted_update,
        pr_opened_update_fn=pr_opened_update,
        run_failed_update_fn=run_failed_update,
        run_requeued_capability_update_fn=run_requeued_capability_mismatch_update,
        run_requeued_stale_snapshot_update_fn=run_requeued_stale_snapshot_update,
        finalize_cancelled_run_fn=finalize_cancelled_run,
        finalize_workflow_result_fn=finalize_workflow_result,
        requeue_workflow_result_for_capability_fn=requeue_workflow_result_for_capability,
        requeue_workflow_result_for_stale_snapshot_fn=requeue_workflow_result_for_stale_snapshot,
        check_run_snapshot_freshness_fn=check_run_snapshot_freshness,
        transition_issue_status_fn=_transition_issue_status,
        emit_agent_event_fn=_emit_agent_event,
        resolve_agent_id_fn=lambda: get_settings().agent_id,
        run_status_queued=RUN_STATUS_QUEUED,
        run_status_running=RUN_STATUS_RUNNING,
        run_status_failed=RUN_STATUS_FAILED,
        run_status_blocked=RUN_STATUS_BLOCKED,
        run_status_cancelled=RUN_STATUS_CANCELLED,
    )


def process_next_queued_run_with_dependencies(
    *,
    session: Session,
    runner: WorkflowRunner,
    send_discord_message_fn=send_tenant_discord_message,
) -> Run | None:
    return _process_next_queued_run_impl(
        session=session,
        runner=runner,
        send_discord_message_fn=send_discord_message_fn,
    )
