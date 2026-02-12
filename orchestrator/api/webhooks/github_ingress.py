from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from orchestrator.api.webhooks.payload_utils import read_json_payload as _read_json_payload
from orchestrator.api.webhooks.contracts import (
    extract_delivery_id as _extract_delivery_id,
    extract_installation_id as _extract_installation_id,
    extract_pull_request_targets as _extract_pull_request_targets,
    extract_repository_full_name as _extract_repository_full_name,
    find_tenant_by_installation_id as _find_tenant_by_installation_id,
    resolve_active_project_for_repo as _resolve_active_project_for_repo,
    resolve_global_github_webhook_secret as _resolve_global_github_webhook_secret,
    resolve_tenant_github_webhook_secret as _resolve_tenant_github_webhook_secret,
    validate_github_webhook_signature as _validate_github_webhook_signature,
)
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.reviewer import ReviewAgentGate
from orchestrator.core.secret_manager import resolve_scoped_secret_ref
from orchestrator.tools.github_app import GitHubApiError, github_client_from_tenant_config

logger = logging.getLogger(__name__)


async def ingest_github_webhook_event(
    *,
    request: Request,
    session: Session,
    settings,  # noqa: ANN001
    request_id: str | None = None,
) -> JSONResponse:
    request_id = request_id or request.headers.get("X-Request-Id") or str(uuid4())
    delivery_id = _extract_delivery_id(request) or str(uuid4())
    github_event = (request.headers.get("X-GitHub-Event") or "").strip().lower()

    logger.info(
        "github_webhook_received request_id=%s delivery_id=%s event=%s",
        request_id,
        delivery_id,
        github_event or "unknown",
    )

    payload, payload_bytes = await _read_json_payload(request, request_id=request_id, source="github")
    global_secret = _resolve_global_github_webhook_secret(
        request_id=request_id,
        session=session,
        settings=settings,
    )
    if global_secret is not None:
        _validate_github_webhook_signature(
            request=request,
            payload_bytes=payload_bytes,
            shared_secret=global_secret,
            request_id=request_id,
            tenant_id=None,
        )

    if github_event == "ping":
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "request_id": request_id,
                "delivery_id": delivery_id,
                "event": github_event,
                "accepted": True,
                "reason": "ping",
            },
        )

    installation_id = _extract_installation_id(payload)
    if installation_id is None:
        logger.warning(
            "github_webhook_invalid_payload request_id=%s delivery_id=%s reason=missing_installation_id",
            request_id,
            delivery_id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing installation identifier",
        )

    tenant = _find_tenant_by_installation_id(session, installation_id=installation_id)
    if tenant is None:
        logger.warning(
            "github_webhook_unknown_installation request_id=%s delivery_id=%s installation_id=%s",
            request_id,
            delivery_id,
            installation_id,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": request_id,
                "delivery_id": delivery_id,
                "event": github_event,
                "installation_id": installation_id,
                "accepted": False,
                "reason": "unknown_installation",
            },
        )

    if not tenant.is_enabled:
        logger.info(
            "github_webhook_ignored request_id=%s delivery_id=%s tenant_id=%s reason=tenant_disabled",
            request_id,
            delivery_id,
            tenant.tenant_id,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": request_id,
                "delivery_id": delivery_id,
                "tenant_id": tenant.tenant_id,
                "event": github_event,
                "accepted": False,
                "reason": "tenant_disabled",
            },
        )

    if global_secret is None:
        tenant_secret = _resolve_tenant_github_webhook_secret(
            tenant=tenant,
            request_id=request_id,
            session=session,
            settings=settings,
        )
        if tenant_secret is not None:
            _validate_github_webhook_signature(
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
        "check_suite",
        "check_run",
    }
    if github_event not in review_events:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": request_id,
                "delivery_id": delivery_id,
                "tenant_id": tenant.tenant_id,
                "event": github_event,
                "action": normalized_action,
                "accepted": True,
                "reason": "ignored_event",
            },
        )

    repo_full_name = _extract_repository_full_name(payload)
    pr_targets = _extract_pull_request_targets(payload)
    if repo_full_name is None or not pr_targets:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": request_id,
                "delivery_id": delivery_id,
                "tenant_id": tenant.tenant_id,
                "event": github_event,
                "action": normalized_action,
                "accepted": False,
                "reason": "missing_pr_context",
            },
        )
    project = _resolve_active_project_for_repo(
        session=session,
        tenant_id=tenant.tenant_id,
        repo_full_name=repo_full_name,
    )
    if project is None:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": request_id,
                "delivery_id": delivery_id,
                "tenant_id": tenant.tenant_id,
                "event": github_event,
                "action": normalized_action,
                "accepted": False,
                "reason": "project_not_mapped",
                "repository": repo_full_name,
            },
        )

    def secret_lookup(secret_ref: str) -> str | None:
        return resolve_scoped_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
        )

    try:
        github_client = github_client_from_tenant_config(
            tenant.github_config,
            secret_lookup=secret_lookup,
        )
        reviewer_gate = ReviewAgentGate(github_client)
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

    signals: list[dict[str, object]] = []
    for pr_number, review_summary_present in pr_targets:
        try:
            signal = reviewer_gate.evaluate_pr(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                review_summary_present=review_summary_present,
            )
        except (GitHubApiError, ValueError) as exc:
            logger.warning(
                "github_webhook_review_failed request_id=%s tenant_id=%s pr_number=%s error=%s",
                request_id,
                tenant.tenant_id,
                pr_number,
                exc,
            )
            signals.append(
                {
                    "pr_number": pr_number,
                    "accepted": False,
                    "error": str(exc),
                }
            )
            continue

        signals.append(
            {
                "pr_number": pr_number,
                "accepted": True,
                "gate": signal.ready,
                "status": signal.state,
                "summary": signal.message,
            }
        )

        if signal.ready:
            send_tenant_discord_message(
                session,
                tenant_id=tenant.tenant_id,
                event="pr_review_gate",
                message=signal.message,
                issue_key=None,
                run_id=None,
                project_id=project.project_id,
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
            "signals": signals,
        },
    )
