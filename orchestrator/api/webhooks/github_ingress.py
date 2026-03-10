from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from orchestrator.api.webhooks.github_review_flow import process_pull_request_targets
from orchestrator.api.webhooks.github_webhook_context import (
    build_github_review_runtime,
    resolve_github_webhook_context,
)
from orchestrator.api.webhooks.pr_review_comment_service import (
    publish_inline_review_batch,
    upsert_sticky_remediation_comment,
    upsert_sticky_review_comment,
)
from orchestrator.api.webhooks.pr_remediation_service import enqueue_pr_remediation_if_needed
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.jira_links import tenant_jira_issue_url
from orchestrator.core.pr_review_findings import evaluate_pr_review_findings
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.tools.github_app import GitHubApiError

logger = logging.getLogger(__name__)


async def ingest_github_webhook_event(
    *,
    request: Request,
    session: Session,
    settings,  # noqa: ANN001
    request_id: str | None = None,
) -> JSONResponse:
    request_id = request_id or request.headers.get("X-Request-Id") or str(uuid4())
    context = await resolve_github_webhook_context(
        request=request,
        session=session,
        settings=settings,
        request_id=request_id,
        logger=logger,
    )
    if isinstance(context, JSONResponse):
        return context
    delivery_id = context.delivery_id
    github_event = context.github_event
    payload = context.payload
    normalized_action = context.normalized_action
    tenant = context.tenant
    project = context.project
    repo_full_name = context.repo_full_name
    pr_targets = context.pr_targets

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
            request_id,
            tenant.tenant_id,
            exc,
        )
        return JSONResponse(
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

    effective_policy = resolve_effective_policy(
        tenant_policy=getattr(tenant, "policy_config", {}) or {},
        project_overrides=getattr(project, "policy_overrides", {}) or {},
    )
    allow_auto_merge = bool(effective_policy.get("allow_auto_merge"))
    max_pr_auto_remediation_loops = _coerce_positive_int(
        effective_policy.get("max_pr_auto_remediation_loops"),
        default=5,
    )

    review_results = process_pull_request_targets(
        request_id=request_id,
        tenant=tenant,
        project=project,
        repo_full_name=repo_full_name,
        pr_targets=pr_targets,
        payload=payload,
        github_event=github_event,
        normalized_action=normalized_action,
        github_client=github_client,
        reviewer_gate=reviewer_gate,
        allow_auto_merge=allow_auto_merge,
        max_pr_auto_remediation_loops=max_pr_auto_remediation_loops,
        session=session,
        settings=settings,
        logger=logger,
        send_tenant_discord_message_fn=send_tenant_discord_message,
        tenant_jira_issue_url_fn=tenant_jira_issue_url,
        evaluate_pr_review_findings_fn=evaluate_pr_review_findings,
        upsert_sticky_review_comment_fn=upsert_sticky_review_comment,
        publish_inline_review_batch_fn=publish_inline_review_batch,
        enqueue_pr_remediation_if_needed_fn=enqueue_pr_remediation_if_needed,
        upsert_sticky_remediation_comment_fn=upsert_sticky_remediation_comment,
        github_api_error_type=GitHubApiError,
    )

    return JSONResponse(
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
            **review_results,
        },
    )


def _coerce_positive_int(value: object | None, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _coerce_int_or_none(value: object | None) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _valid_pr_details(details: object) -> bool:
    head_sha = getattr(details, "head_sha", None)
    title = getattr(details, "title", None)
    return isinstance(head_sha, str) and bool(head_sha.strip()) and isinstance(title, str) and bool(title.strip())
