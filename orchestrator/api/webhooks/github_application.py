from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from orchestrator.api.webhooks.github_event_classifier import resolve_github_policy_state
from orchestrator.api.webhooks.github_manual_fix_planner import plan_manual_fix_reaction_actions
from orchestrator.api.webhooks.github_review_planner import plan_pull_request_targets
from orchestrator.api.webhooks.github_webhook_context import (
    build_github_review_runtime,
    resolve_github_webhook_context,
)
from orchestrator.api.webhooks.pr_remediation_service import enqueue_pr_remediation_if_needed
from orchestrator.core.communications import (
    HttpJsonResponseAction,
    HttpJsonResponseBytesAction,
    IngressResult,
    TransportEnvelope,
)
from orchestrator.core.communications.integration_contracts import TransportActionExecutor
from orchestrator.core.github.transport_executor import GitHubTransportExecutor
from orchestrator.core.jira_links import tenant_jira_issue_url
from orchestrator.core.pr_review_findings import evaluate_pr_review_findings

logger = logging.getLogger(__name__)


async def build_github_webhook_ingress_result(
    *,
    request,
    session: Session,
    settings,  # noqa: ANN001
    envelope: TransportEnvelope,
    register_transport_executor: Callable[[TransportActionExecutor], None] | None = None,
) -> IngressResult:
    context = await resolve_github_webhook_context(
        request=request,
        session=session,
        settings=settings,
        request_id=envelope.request_id,
        logger=logger,
    )
    if isinstance(context, JSONResponse):
        return _json_response_result(context)

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

    if not policy_state.allow_code_reviews and not policy_state.manual_fix_requested:
        return _http_json_result(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": envelope.request_id,
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
                "remediation": [],
                "remediation_comments": [],
            },
        )

    try:
        github_client, reviewer_gate = build_github_review_runtime(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
        )
    except ValueError as exc:
        logger.warning(
            "github_webhook_review_misconfigured request_id=%s tenant_id=%s error=%s",
            envelope.request_id,
            tenant.tenant_id,
            exc,
        )
        return _http_json_result(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": envelope.request_id,
                "delivery_id": delivery_id,
                "tenant_id": tenant.tenant_id,
                "event": github_event,
                "action": normalized_action,
                "accepted": False,
                "reason": "review_misconfigured",
                "project_id": project.project_id,
            },
    )

    if register_transport_executor is not None:
        register_transport_executor(
            GitHubTransportExecutor(
                github_client=github_client,
                session=session,
                logger_override=logger,
            )
        )

    planned_actions = plan_manual_fix_reaction_actions(
        github_event=github_event,
        payload=payload,
        repo_full_name=repo_full_name,
    )

    review_plan = plan_pull_request_targets(
        request_id=envelope.request_id,
        tenant=tenant,
        project=project,
        repo_full_name=repo_full_name,
        pr_targets=pr_targets,
        payload=payload,
        github_event=github_event,
        normalized_action=normalized_action,
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
            "request_id": envelope.request_id,
            "delivery_id": delivery_id,
            "tenant_id": tenant.tenant_id,
            "project_id": project.project_id,
            "event": github_event,
            "action": normalized_action,
            "accepted": True,
            "repository": repo_full_name,
            **review_plan.summary,
        },
        extra_actions=(*planned_actions, *review_plan.actions),
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
