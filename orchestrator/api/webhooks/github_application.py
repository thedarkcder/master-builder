from __future__ import annotations

import logging

from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from orchestrator.api.webhooks.github_review_flow import process_pull_request_targets
from orchestrator.api.webhooks.github_webhook_context import (
    build_github_review_runtime,
    resolve_github_webhook_context,
)
from orchestrator.api.webhooks.pr_remediation_policy import parse_manual_pr_fix_request
from orchestrator.api.webhooks.pr_remediation_service import enqueue_pr_remediation_if_needed
from orchestrator.api.webhooks.pr_review_comment_service import (
    publish_inline_review_batch,
    upsert_manual_fix_followup_comment,
    upsert_sticky_remediation_comment,
    upsert_sticky_review_comment,
)
from orchestrator.core.communications import (
    GitHubIssueCommentReactionAction,
    GitHubPullRequestReviewCommentReactionAction,
    HttpJsonResponseAction,
    HttpJsonResponseBytesAction,
    IngressResult,
    TransportEnvelope,
)
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.jira_links import tenant_jira_issue_url
from orchestrator.core.pr_review_findings import evaluate_pr_review_findings
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.tools.github_app import GitHubApiError

logger = logging.getLogger(__name__)


async def build_github_webhook_ingress_result(
    *,
    request,
    session: Session,
    settings,  # noqa: ANN001
    envelope: TransportEnvelope,
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

    effective_policy = resolve_effective_policy(
        tenant_policy=getattr(tenant, "policy_config", {}) or {},
        project_overrides=getattr(project, "policy_overrides", {}) or {},
    )
    allow_code_reviews = bool(effective_policy.get("allow_code_reviews", True))
    allow_auto_merge = bool(effective_policy.get("allow_auto_merge"))
    allow_pr_remediation = allow_code_reviews and bool(effective_policy.get("allow_pr_remediation", True))
    allow_manual_pr_fix_requests = bool(effective_policy.get("allow_manual_pr_fix_requests", True))
    manual_fix_requested = (
        github_event in {"issue_comment", "pull_request_review_comment"}
        and parse_manual_pr_fix_request(payload=payload) is not None
    )
    max_pr_auto_remediation_loops = _coerce_positive_int(
        effective_policy.get("max_pr_auto_remediation_loops"),
        default=5,
    )

    if not allow_code_reviews and not manual_fix_requested:
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
                    "manual_fix_requests_enabled": allow_manual_pr_fix_requests,
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

    _add_manual_fix_eyes_reaction_if_requested(
        github_event=github_event,
        payload=payload,
        github_client=github_client,
        repo_full_name=repo_full_name,
        logger=logger,
        request_id=envelope.request_id,
        tenant_id=tenant.tenant_id,
    )

    review_results = process_pull_request_targets(
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
        allow_auto_merge=allow_auto_merge,
        allow_pr_remediation=allow_pr_remediation,
        allow_manual_pr_fix_requests=allow_manual_pr_fix_requests,
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
        upsert_manual_fix_followup_comment_fn=upsert_manual_fix_followup_comment,
        github_api_error_type=GitHubApiError,
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
            **review_results,
        },
    )


def _http_json_result(*, status_code: int, content: dict) -> IngressResult:
    return IngressResult(
        actions=(
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


def _coerce_positive_int(value: object | None, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _add_manual_fix_eyes_reaction_if_requested(
    *,
    github_event: str,
    payload: dict,
    github_client,
    repo_full_name: str,
    logger,
    request_id: str,
    tenant_id: str,
) -> None:  # noqa: ANN001
    normalized_event = str(github_event or "").strip().lower()
    if normalized_event not in {"issue_comment", "pull_request_review_comment"}:
        return
    if parse_manual_pr_fix_request(payload=payload) is None:
        return
    comment = payload.get("comment")
    comment_id = comment.get("id") if isinstance(comment, dict) else None
    if not isinstance(comment_id, int) or comment_id <= 0:
        return
    try:
        action = (
            GitHubIssueCommentReactionAction(
                repo_full_name=repo_full_name,
                comment_id=comment_id,
                content="eyes",
            )
            if normalized_event == "issue_comment"
            else GitHubPullRequestReviewCommentReactionAction(
                repo_full_name=repo_full_name,
                comment_id=comment_id,
                content="eyes",
            )
        )
        GitHubTransportExecutor(github_client=github_client).execute(action=action)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "github_webhook_manual_fix_reaction_failed request_id=%s tenant_id=%s comment_id=%s error=%s",
            request_id,
            tenant_id,
            comment_id,
            exc,
        )


class GitHubTransportExecutor:
    def __init__(self, *, github_client) -> None:  # noqa: ANN001
        self._github_client = github_client

    def execute(self, *, action) -> None:  # noqa: ANN001
        if isinstance(action, GitHubIssueCommentReactionAction):
            self._github_client.add_issue_comment_reaction(
                repo_full_name=action.repo_full_name,
                comment_id=action.comment_id,
                content=action.content,
            )
            return
        if isinstance(action, GitHubPullRequestReviewCommentReactionAction):
            self._github_client.add_pull_request_review_comment_reaction(
                repo_full_name=action.repo_full_name,
                comment_id=action.comment_id,
                content=action.content,
            )
            return
        raise RuntimeError(f"Unsupported GitHub transport action: {type(action).__name__}")
