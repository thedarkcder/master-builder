from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.core.runs import enqueue_run
from orchestrator.storage.models import Tenant

router = APIRouter(tags=["jira-webhook"])

logger = logging.getLogger(__name__)
DEFAULT_WEBHOOK_MAX_BODY_BYTES = 1_048_576


def _max_webhook_body_bytes() -> int:
    raw_value = os.environ.get("ORCHESTRATOR_WEBHOOK_MAX_BODY_BYTES", str(DEFAULT_WEBHOOK_MAX_BODY_BYTES))
    try:
        parsed = int(raw_value)
        if parsed <= 0:
            raise ValueError
    except ValueError:
        logger.warning(
            "invalid_webhook_max_body_bytes value=%s default=%s",
            raw_value,
            DEFAULT_WEBHOOK_MAX_BODY_BYTES,
        )
        return DEFAULT_WEBHOOK_MAX_BODY_BYTES
    return parsed


async def _read_json_payload(
    request: Request,
    *,
    request_id: str,
    source: str,
) -> tuple[dict, bytes]:
    body = await request.body()
    max_bytes = _max_webhook_body_bytes()
    if len(body) > max_bytes:
        logger.warning(
            "%s_webhook_payload_too_large request_id=%s body_bytes=%s max_bytes=%s",
            source,
            request_id,
            len(body),
            max_bytes,
        )
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Payload too large",
        )

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload")
    return payload, body


def _extract_issue_payload(payload: dict) -> tuple[str, list[str]]:
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing issue object")

    issue_key = issue.get("key")
    if not isinstance(issue_key, str) or not issue_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing issue key")

    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    labels = fields.get("labels") if isinstance(fields, dict) else []
    if not isinstance(labels, list):
        labels = []

    normalized_labels = [str(label) for label in labels]
    return issue_key, normalized_labels


def _extract_webhook_token(request: Request) -> str | None:
    webhook_token = request.headers.get("X-Webhook-Token")
    if webhook_token:
        return webhook_token.strip()

    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:].strip()

    return None


def _extract_delivery_id(request: Request) -> str | None:
    header_candidates = (
        "X-Atlassian-Webhook-Identifier",
        "X-Webhook-Delivery",
        "X-GitHub-Delivery",
    )
    for header_name in header_candidates:
        value = request.headers.get(header_name)
        if value:
            normalized = value.strip()
            if normalized:
                return normalized
    return None


def _validate_webhook_auth(tenant: Tenant, request: Request, request_id: str) -> None:
    webhook_secret_ref = tenant.jira_config.get("webhook_secret_ref")
    if not webhook_secret_ref:
        return

    expected_token = os.environ.get(str(webhook_secret_ref))
    if not expected_token:
        logger.error(
            "jira_webhook_auth_misconfigured request_id=%s tenant_id=%s secret_ref=%s",
            request_id,
            tenant.tenant_id,
            webhook_secret_ref,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook authentication is misconfigured",
        )

    presented_token = _extract_webhook_token(request)
    if not presented_token or not secrets.compare_digest(presented_token, expected_token):
        logger.warning(
            "jira_webhook_auth_failed request_id=%s tenant_id=%s",
            request_id,
            tenant.tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook token",
        )


def _resolve_global_github_webhook_secret(*, request_id: str) -> str | None:
    secret_ref = (os.environ.get("ORCHESTRATOR_GITHUB_WEBHOOK_SECRET_REF") or "").strip()
    if not secret_ref:
        return None

    secret_value = os.environ.get(secret_ref)
    if not secret_value:
        logger.error(
            "github_webhook_auth_misconfigured request_id=%s secret_ref=%s",
            request_id,
            secret_ref,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GitHub webhook authentication is misconfigured",
        )
    return secret_value


def _resolve_tenant_github_webhook_secret(*, tenant: Tenant, request_id: str) -> str | None:
    webhook_secret_ref = tenant.github_config.get("webhook_secret_ref")
    if not webhook_secret_ref:
        return None

    secret_value = os.environ.get(str(webhook_secret_ref))
    if not secret_value:
        logger.error(
            "github_webhook_auth_misconfigured request_id=%s tenant_id=%s secret_ref=%s",
            request_id,
            tenant.tenant_id,
            webhook_secret_ref,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GitHub webhook authentication is misconfigured",
        )
    return secret_value


def _validate_github_webhook_signature(
    *,
    request: Request,
    payload_bytes: bytes,
    shared_secret: str,
    request_id: str,
    tenant_id: str | None,
) -> None:
    presented_signature = (request.headers.get("X-Hub-Signature-256") or "").strip()
    if not presented_signature.startswith("sha256="):
        logger.warning(
            "github_webhook_auth_failed request_id=%s tenant_id=%s reason=missing_or_invalid_signature_header",
            request_id,
            tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid GitHub webhook signature",
        )

    digest = hmac.new(shared_secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    expected_signature = f"sha256={digest}"
    if not secrets.compare_digest(presented_signature, expected_signature):
        logger.warning(
            "github_webhook_auth_failed request_id=%s tenant_id=%s reason=signature_mismatch",
            request_id,
            tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid GitHub webhook signature",
        )


def _extract_installation_id(payload: dict) -> str | None:
    installation = payload.get("installation")
    if isinstance(installation, dict):
        installation_id = installation.get("id")
        if isinstance(installation_id, int):
            return str(installation_id)
        if isinstance(installation_id, str) and installation_id.strip():
            return installation_id.strip()

    installation_id_fallback = payload.get("installation_id")
    if isinstance(installation_id_fallback, int):
        return str(installation_id_fallback)
    if isinstance(installation_id_fallback, str) and installation_id_fallback.strip():
        return installation_id_fallback.strip()
    return None


def _find_tenant_by_installation_id(session: Session, installation_id: str) -> Tenant | None:
    tenants = session.execute(select(Tenant)).scalars().all()
    for tenant in tenants:
        configured_installation_id = str(tenant.github_config.get("installation_id") or "").strip()
        if configured_installation_id and configured_installation_id == installation_id:
            return tenant
    return None


@router.post("/jira/webhook/{tenant_id}")
async def ingest_jira_webhook(
    tenant_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    request_id = request.headers.get("X-Request-Id") or str(uuid4())
    logger.info("jira_webhook_received request_id=%s tenant_id=%s", request_id, tenant_id)

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        logger.warning("jira_webhook_unknown_tenant request_id=%s tenant_id=%s", request_id, tenant_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tenant")
    if not tenant.is_enabled:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s reason=tenant_disabled",
            request_id,
            tenant_id,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "enqueued": False,
            "reason": "tenant_disabled",
        }

    _validate_webhook_auth(tenant=tenant, request=request, request_id=request_id)

    payload, _ = await _read_json_payload(request, request_id=request_id, source="jira")

    issue_key, labels = _extract_issue_payload(payload)
    delivery_id = _extract_delivery_id(request)
    logger.info(
        "jira_webhook_issue_parsed request_id=%s tenant_id=%s issue_key=%s delivery_id=%s",
        request_id,
        tenant_id,
        issue_key,
        delivery_id,
    )

    ready_label = tenant.jira_config.get("ready_label", "agent:ready")
    if ready_label not in labels:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=ready_label_missing",
            request_id,
            tenant_id,
            issue_key,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "issue_key": issue_key,
            "enqueued": False,
            "reason": "ready_label_missing",
        }

    enqueue_result = enqueue_run(
        session,
        tenant_id=tenant_id,
        issue_key=issue_key,
        delivery_id=delivery_id,
        max_concurrent_runs=tenant.policy_config.get("max_concurrent_runs"),
    )
    if not enqueue_result.enqueued:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=%s run_id=%s",
            request_id,
            tenant_id,
            issue_key,
            enqueue_result.reason,
            enqueue_result.run.run_id,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "issue_key": issue_key,
            "enqueued": False,
            "reason": enqueue_result.reason,
            "run_id": enqueue_result.run.run_id,
        }
    logger.info(
        "jira_webhook_enqueued request_id=%s tenant_id=%s issue_key=%s run_id=%s",
        request_id,
        tenant_id,
        issue_key,
        enqueue_result.run.run_id,
    )

    return {
        "request_id": request_id,
        "tenant_id": tenant_id,
        "issue_key": issue_key,
        "enqueued": True,
        "run_id": enqueue_result.run.run_id,
    }


@router.post("/github/webhook")
async def ingest_github_webhook(
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    request_id = request.headers.get("X-Request-Id") or str(uuid4())
    delivery_id = _extract_delivery_id(request) or str(uuid4())
    github_event = (request.headers.get("X-GitHub-Event") or "").strip().lower()

    logger.info(
        "github_webhook_received request_id=%s delivery_id=%s event=%s",
        request_id,
        delivery_id,
        github_event or "unknown",
    )

    payload, payload_bytes = await _read_json_payload(request, request_id=request_id, source="github")
    global_secret = _resolve_global_github_webhook_secret(request_id=request_id)
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
        tenant_secret = _resolve_tenant_github_webhook_secret(tenant=tenant, request_id=request_id)
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
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "request_id": request_id,
            "delivery_id": delivery_id,
            "tenant_id": tenant.tenant_id,
            "event": github_event,
            "action": normalized_action,
            "accepted": True,
            "reason": "accepted_no_handler",
        },
    )
