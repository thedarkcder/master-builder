from __future__ import annotations

from orchestrator.core.communications import DiscordTenantNotificationAction
from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.run_gate_service import enqueue_issue_run_with_precheck


def plan_jira_enqueue(
    *,
    session,
    context,
    issue_description: str | None,
    precheck_decision,
) -> object:  # noqa: ANN001
    return enqueue_issue_run_with_precheck(
        session,
        tenant_id=context.tenant_id,
        project_id=context.project.project_id,
        issue_key=context.issue_key,
        issue_summary=context.issue_summary,
        issue_description=issue_description,
        repo_url=context.project.github_repository,
        delivery_id=context.delivery_id,
        precheck_outcome=precheck_decision.pre_check.outcome if precheck_decision.pre_check is not None else None,
        max_concurrent_runs=context.tenant.policy_config.get("max_concurrent_runs"),
    )


def build_jira_enqueue_skipped_notification_action(
    *,
    context,
    reason: str,
    extra_detail: str | None = None,
) -> DiscordTenantNotificationAction:
    detail = f" ({extra_detail})" if extra_detail else ""
    guidance = enqueue_reason_guidance(reason)
    message = (
        f"Jira webhook did not queue a run for `{context.issue_key}`.\n"
        f"Reason: `{reason}`{detail}\n"
        f"Guidance: {guidance}\n"
        f"Status: `{context.issue_status or 'unknown'}`"
    )
    return DiscordTenantNotificationAction(
        tenant_id=context.tenant_id,
        project_id=context.project.project_id if context.project is not None else None,
        message=message,
    )
