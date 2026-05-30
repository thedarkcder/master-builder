from __future__ import annotations

import logging

from fastapi import status
from fastapi.responses import JSONResponse

from orchestrator.api.webhooks.github_event_classifier import (
    classify_github_trigger_state,
    empty_github_review_summary,
    resolve_github_policy_state,
)
from orchestrator.api.webhooks.github_staging_admission import plan_staging_admission_actions
from orchestrator.api.webhooks.github_manual_fix_planner import plan_manual_fix_reaction_actions
from orchestrator.api.webhooks.github_review_planner import plan_pull_request_targets
from orchestrator.api.webhooks.github_webhook_context import (
    GitHubWebhookPreparedRuntime,
)
from orchestrator.api.webhooks.pr_remediation_service import enqueue_pr_remediation_if_needed
from orchestrator.core.communications import (
    HttpJsonResponseAction,
    HttpJsonResponseBytesAction,
    IngressResult,
)
from orchestrator.core.integrations.atlassian.links import tenant_jira_issue_url
from orchestrator.core.review.pr_review_findings import evaluate_pr_review_findings

logger = logging.getLogger(__name__)


async def build_github_webhook_ingress_result(
    *,
    prepared_runtime: GitHubWebhookPreparedRuntime | JSONResponse,
    request_id: str,
    session,
    settings,  # noqa: ANN001
) -> IngressResult:
    if isinstance(prepared_runtime, JSONResponse):
        return _json_response_result(prepared_runtime)

    context = prepared_runtime.context
    delivery_id = context.delivery_id
    github_event = context.github_event
    payload = context.payload
    normalized_action = context.normalized_action
    tenant = context.tenant
    project = context.project
    repo_full_name = context.repo_full_name
    pr_targets = context.pr_targets

    policy_state = resolve_github_policy_state(
        tenant_policy=getattr(tenant, "policy_config", {}) or {},
        project_overrides=getattr(project, "policy_overrides", {}) or {},
        github_event=github_event,
        payload=payload,
    )
    trigger_state = classify_github_trigger_state(
        github_event=github_event,
        normalized_action=normalized_action,
        payload=payload,
    )
    staging_admission_plan = None
    if prepared_runtime.github_client is not None:
        staging_admission_plan = plan_staging_admission_actions(
            github_event=github_event,
            normalized_action=normalized_action,
            payload=payload,
            repo_full_name=repo_full_name,
            project_overrides=getattr(project, "policy_overrides", {}) or {},
            pr_targets=pr_targets,
            github_client=prepared_runtime.github_client,
        )

    if (
        not policy_state.allow_code_reviews
        and not trigger_state.manual_fix_requested
        and not bool(getattr(staging_admission_plan, "actions", ()))
    ):
        return _http_json_result(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": request_id,
                "delivery_id": delivery_id,
                "tenant_id": tenant.tenant_id,
                "project_id": project.project_id,
                "event": github_event,
                "action": normalized_action,
                "accepted": True,
                "repository": repo_full_name,
                "signals": [],
                "review_comments": [],
                "inline_reviews": [],
                "auto_merge": {
                    "enabled": False,
                    "reason": "code_reviews_disabled",
                    "results": [],
                },
                "pr_review": {
                    "enabled": False,
                    "reason": "code_reviews_disabled",
                },
                "pr_remediation": {
                    "enabled": False,
                    "manual_fix_requests_enabled": policy_state.allow_manual_pr_fix_requests,
                    "reason": "code_reviews_disabled",
                },
                "staging_admission": {
                    "enabled": False,
                    "results": [],
                },
                "remediation": [],
                "remediation_comments": [],
            },
        )

    planned_actions = plan_manual_fix_reaction_actions(
        github_event=github_event,
        payload=payload,
        repo_full_name=repo_full_name,
    )
    if trigger_state.ignored_reason is not None and not bool(getattr(staging_admission_plan, "actions", ())):
        logger.info(
            "github_review_trigger_ignored request_id=%s tenant_id=%s project_id=%s repo=%s event=%s action=%s reason=%s sender=%s",
            request_id,
            tenant.tenant_id,
            project.project_id,
            repo_full_name,
            github_event,
            normalized_action or "none",
            trigger_state.ignored_reason,
            trigger_state.sender_login or "unknown",
        )
        return _http_json_result(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": request_id,
                "delivery_id": delivery_id,
                "tenant_id": tenant.tenant_id,
                "project_id": project.project_id,
                "event": github_event,
                "action": normalized_action,
                "accepted": True,
                "repository": repo_full_name,
                **empty_github_review_summary(
                    allow_auto_merge=policy_state.allow_auto_merge,
                    allow_pr_remediation=policy_state.allow_pr_remediation,
                    allow_manual_pr_fix_requests=policy_state.allow_manual_pr_fix_requests,
                    full_review_trigger=False,
                    ignored_reason=trigger_state.ignored_reason,
                ),
                "staging_admission": {
                    "enabled": False,
                    "results": [],
                },
            },
            extra_actions=planned_actions,
        )
    if (
        not trigger_state.full_review_trigger
        and not trigger_state.remediation_trigger
        and not trigger_state.manual_fix_requested
        and not bool(getattr(staging_admission_plan, "actions", ()))
    ):
        logger.info(
            "github_review_trigger_ignored request_id=%s tenant_id=%s project_id=%s repo=%s event=%s action=%s reason=unsupported_event sender=%s",
            request_id,
            tenant.tenant_id,
            project.project_id,
            repo_full_name,
            github_event,
            normalized_action or "none",
            trigger_state.sender_login or "unknown",
        )
        return _http_json_result(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": request_id,
                "delivery_id": delivery_id,
                "tenant_id": tenant.tenant_id,
                "project_id": project.project_id,
                "event": github_event,
                "action": normalized_action,
                "accepted": True,
                "repository": repo_full_name,
                **empty_github_review_summary(
                    allow_auto_merge=policy_state.allow_auto_merge,
                    allow_pr_remediation=policy_state.allow_pr_remediation,
                    allow_manual_pr_fix_requests=policy_state.allow_manual_pr_fix_requests,
                    full_review_trigger=False,
                    ignored_reason="unsupported_event",
                ),
                "staging_admission": {
                    "enabled": False,
                    "results": [],
                },
            },
            extra_actions=planned_actions,
        )

    if prepared_runtime.review_runtime_error is not None:
        logger.warning(
            "github_webhook_review_misconfigured request_id=%s tenant_id=%s error=%s",
            request_id,
            tenant.tenant_id,
            prepared_runtime.review_runtime_error,
        )
        return _http_json_result(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": request_id,
                "delivery_id": delivery_id,
                "tenant_id": tenant.tenant_id,
                "event": github_event,
                "action": normalized_action,
                "accepted": False,
                "reason": "review_misconfigured",
                "project_id": project.project_id,
            },
        )

    github_client = prepared_runtime.github_client
    reviewer_gate = prepared_runtime.reviewer_gate
    review_plan = plan_pull_request_targets(
        request_id=request_id,
        tenant=tenant,
        project=project,
        repo_full_name=repo_full_name,
        pr_targets=pr_targets,
        payload=payload,
        github_event=github_event,
        normalized_action=normalized_action,
        full_review_trigger=trigger_state.full_review_trigger,
        remediation_trigger=trigger_state.remediation_trigger,
        manual_fix_requested=trigger_state.manual_fix_requested,
        github_client=github_client,
        reviewer_gate=reviewer_gate,
        allow_auto_merge=policy_state.allow_auto_merge,
        allow_pr_remediation=policy_state.allow_pr_remediation,
        allow_manual_pr_fix_requests=policy_state.allow_manual_pr_fix_requests,
        max_pr_auto_remediation_loops=policy_state.max_pr_auto_remediation_loops,
        session=session,
        settings=settings,
        logger=logger,
        tenant_jira_issue_url_fn=tenant_jira_issue_url,
        evaluate_pr_review_findings_fn=evaluate_pr_review_findings,
        enqueue_pr_remediation_if_needed_fn=enqueue_pr_remediation_if_needed,
    )

    return _http_json_result(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "request_id": request_id,
            "delivery_id": delivery_id,
            "tenant_id": tenant.tenant_id,
            "project_id": project.project_id,
            "event": github_event,
            "action": normalized_action,
            "accepted": True,
            "repository": repo_full_name,
            **review_plan.summary,
            "staging_admission": {
                "enabled": bool(getattr(staging_admission_plan, "enabled", False)),
                "results": list(getattr(staging_admission_plan, "results", [])),
            },
        },
        extra_actions=(
            *planned_actions,
            *review_plan.actions,
            *tuple(getattr(staging_admission_plan, "actions", ()) or ()),
        ),
    )


def _http_json_result(*, status_code: int, content: dict, extra_actions: tuple = ()) -> IngressResult:
    return IngressResult(
        actions=(
            *extra_actions,
            HttpJsonResponseAction(status_code=status_code, content=content),
        )
    )


def _json_response_result(response: JSONResponse) -> IngressResult:
    return IngressResult(
        actions=(
            HttpJsonResponseBytesAction(
                status_code=response.status_code,
                body=response.body,
                headers=dict(response.headers),
            ),
        )
    )
