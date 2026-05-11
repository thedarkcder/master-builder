from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from orchestrator.api.webhooks.payload_utils import read_json_payload
from orchestrator.api.webhooks.contracts import (
    extract_delivery_id,
    extract_installation_id,
    extract_push_deployment_source,
    extract_pull_request_targets,
    extract_repository_full_name,
    find_tenant_by_installation_id,
    resolve_active_project_for_repo,
    resolve_global_github_webhook_secret,
    resolve_tenant_github_webhook_secret,
    validate_github_webhook_signature,
)
from orchestrator.core.communications.integration_contracts import TransportActionExecutor
from orchestrator.core.github.transport_executor import GitHubTransportExecutor
from orchestrator.core.review.reviewer import ReviewAgentGate
from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.core.platform.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.tools.github_app import github_client_from_tenant_config


@dataclass(frozen=True)
class GitHubWebhookContext:
    request_id: str
    delivery_id: str
    github_event: str
    payload: dict
    normalized_action: str | None
    installation_id: int
    tenant: object
    project: object
    repo_full_name: str
    pr_targets: list[tuple[int, bool]]


@dataclass(frozen=True)
class GitHubWebhookPreparedRuntime:
    context: GitHubWebhookContext
    github_client: object | None
    reviewer_gate: ReviewAgentGate | None
    review_runtime_error: str | None = None
    transport_action_executors: tuple[TransportActionExecutor, ...] = ()



def github_response(*, status_code: int, **content) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=content)


async def resolve_github_webhook_context(*, request: Request, session, settings, request_id: str, logger) -> GitHubWebhookContext | JSONResponse:
    delivery_id = extract_delivery_id(request) or str(uuid4())
    github_event = (request.headers.get("X-GitHub-Event") or "").strip().lower()

    logger.info(
        "github_webhook_received request_id=%s delivery_id=%s event=%s",
        request_id,
        delivery_id,
        github_event or "unknown",
    )

    payload, payload_bytes = await read_json_payload(request, request_id=request_id, source="github")
    global_secret = resolve_global_github_webhook_secret(
        request_id=request_id,
        session=session,
        settings=settings,
    )
    if global_secret is not None:
        validate_github_webhook_signature(
            request=request,
            payload_bytes=payload_bytes,
            shared_secret=global_secret,
            request_id=request_id,
            tenant_id=None,
        )

    if github_event == "ping":
        return github_response(
            status_code=status.HTTP_200_OK,
            request_id=request_id,
            delivery_id=delivery_id,
            event=github_event,
            accepted=True,
            reason="ping",
        )

    installation_id = extract_installation_id(payload)
    if installation_id is None:
        logger.warning(
            "github_webhook_invalid_payload request_id=%s delivery_id=%s reason=missing_installation_id",
            request_id,
            delivery_id,
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing installation identifier")

    tenant = find_tenant_by_installation_id(session, installation_id=installation_id)
    if tenant is None:
        logger.warning(
            "github_webhook_unknown_installation request_id=%s delivery_id=%s installation_id=%s",
            request_id,
            delivery_id,
            installation_id,
        )
        return github_response(
            status_code=status.HTTP_202_ACCEPTED,
            request_id=request_id,
            delivery_id=delivery_id,
            event=github_event,
            installation_id=installation_id,
            accepted=False,
            reason="unknown_installation",
        )

    if not tenant.is_enabled:
        logger.info(
            "github_webhook_ignored request_id=%s delivery_id=%s tenant_id=%s reason=tenant_disabled",
            request_id,
            delivery_id,
            tenant.tenant_id,
        )
        return github_response(
            status_code=status.HTTP_202_ACCEPTED,
            request_id=request_id,
            delivery_id=delivery_id,
            tenant_id=tenant.tenant_id,
            event=github_event,
            accepted=False,
            reason="tenant_disabled",
        )

    if global_secret is None:
        tenant_secret = resolve_tenant_github_webhook_secret(
            tenant=tenant,
            request_id=request_id,
            session=session,
            settings=settings,
        )
        if tenant_secret is not None:
            validate_github_webhook_signature(
                request=request,
                payload_bytes=payload_bytes,
                shared_secret=tenant_secret,
                request_id=request_id,
                tenant_id=tenant.tenant_id,
            )

    action = payload.get("action")
    normalized_action = action.strip() if isinstance(action, str) else None
    logger.info(
        "github_webhook_accepted request_id=%s delivery_id=%s tenant_id=%s event=%s action=%s installation_id=%s",
        request_id,
        delivery_id,
        tenant.tenant_id,
        github_event or "unknown",
        normalized_action or "none",
        installation_id,
    )

    review_events = {
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "issue_comment",
        "check_suite",
        "check_run",
    }
    deployment_events = {"push"}
    if github_event not in review_events | deployment_events:
        return github_response(
            status_code=status.HTTP_202_ACCEPTED,
            request_id=request_id,
            delivery_id=delivery_id,
            tenant_id=tenant.tenant_id,
            event=github_event,
            action=normalized_action,
            accepted=True,
            reason="ignored_event",
        )

    repo_full_name = extract_repository_full_name(payload)
    pr_targets = extract_pull_request_targets(payload)
    push_source = extract_push_deployment_source(payload) if github_event == "push" else None
    if repo_full_name is None or (github_event in review_events and not pr_targets):
        return github_response(
            status_code=status.HTTP_202_ACCEPTED,
            request_id=request_id,
            delivery_id=delivery_id,
            tenant_id=tenant.tenant_id,
            event=github_event,
            action=normalized_action,
            accepted=False,
            reason="missing_pr_context",
        )
    if github_event == "push" and push_source is None:
        return github_response(
            status_code=status.HTTP_202_ACCEPTED,
            request_id=request_id,
            delivery_id=delivery_id,
            tenant_id=tenant.tenant_id,
            event=github_event,
            action=normalized_action,
            accepted=False,
            reason="missing_push_deployment_source",
        )

    project = resolve_active_project_for_repo(
        session=session,
        tenant_id=tenant.tenant_id,
        repo_full_name=repo_full_name,
    )
    if project is None:
        return github_response(
            status_code=status.HTTP_202_ACCEPTED,
            request_id=request_id,
            delivery_id=delivery_id,
            tenant_id=tenant.tenant_id,
            event=github_event,
            action=normalized_action,
            accepted=False,
            reason="project_not_mapped",
            repository=repo_full_name,
        )

    return GitHubWebhookContext(
        request_id=request_id,
        delivery_id=delivery_id,
        github_event=github_event,
        payload=payload,
        normalized_action=normalized_action,
        installation_id=installation_id,
        tenant=tenant,
        project=project,
        repo_full_name=repo_full_name,
        pr_targets=pr_targets,
    )



def build_github_review_runtime(*, session, settings, tenant, project):
    github_client = github_client_from_tenant_config(
        tenant.github_config,
        tenant_secret_lookup=lambda secret_ref: resolve_scoped_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
        ),
        platform_secret_lookup=lambda secret_ref: resolve_platform_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
        ),
    )
    reviewer_gate = ReviewAgentGate(
        github_client,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
    )
    return github_client, reviewer_gate


async def prepare_github_webhook_runtime(
    *,
    request: Request,
    session: Session,
    settings,  # noqa: ANN001
    request_id: str,
    logger,
) -> GitHubWebhookPreparedRuntime | JSONResponse:
    context = await resolve_github_webhook_context(
        request=request,
        session=session,
        settings=settings,
        request_id=request_id,
        logger=logger,
    )
    if isinstance(context, JSONResponse):
        return context
    try:
        github_client, reviewer_gate = build_github_review_runtime(
            session=session,
            settings=settings,
            tenant=context.tenant,
            project=context.project,
        )
    except ValueError as exc:
        return GitHubWebhookPreparedRuntime(
            context=context,
            github_client=None,
            reviewer_gate=None,
            review_runtime_error=str(exc),
        )
    return GitHubWebhookPreparedRuntime(
        context=context,
        github_client=github_client,
        reviewer_gate=reviewer_gate,
        transport_action_executors=(
            GitHubTransportExecutor(
                github_client=github_client,
                session=session,
                logger_override=logger,
            ),
        ),
    )
