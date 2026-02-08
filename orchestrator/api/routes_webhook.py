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
from orchestrator.core.config import get_settings
from orchestrator.core.runs import enqueue_run
from orchestrator.core.secret_manager import resolve_secret_ref
from orchestrator.core.signal_templates import format_discord_ready_gate_guidance
from orchestrator.storage.models import Tenant

router = APIRouter(tags=["jira-webhook"])

logger = logging.getLogger(__name__)
DEFAULT_WEBHOOK_MAX_BODY_BYTES = 1_048_576
HTTP_413_TOO_LARGE = getattr(
    status,
    "HTTP_413_CONTENT_TOO_LARGE",
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
)
GLOBAL_GITHUB_WEBHOOK_SECRET_REF = "GITHUB_WEBHOOK_SECRET"


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
            status_code=HTTP_413_TOO_LARGE,
            detail="Payload too large",
        )

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload")
    return payload, body


def _adf_to_text(node: object) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(part for part in (_adf_to_text(item) for item in node) if part).strip()
    if not isinstance(node, dict):
        return ""

    text = node.get("text")
    if isinstance(text, str):
        return text

    content = node.get("content")
    if isinstance(content, list):
        return " ".join(part for part in (_adf_to_text(item) for item in content) if part).strip()
    return ""


def _extract_issue_payload(
    payload: dict,
) -> tuple[str, list[str], str | None, str | None, str | None, str | None]:
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

    summary_raw = fields.get("summary") if isinstance(fields, dict) else None
    summary = str(summary_raw).strip() if isinstance(summary_raw, str) and summary_raw.strip() else None

    description_raw = fields.get("description") if isinstance(fields, dict) else None
    description_text = _adf_to_text(description_raw).strip()
    description = description_text or None

    status_name: str | None = None
    status_category_key: str | None = None
    status_field = fields.get("status")
    if isinstance(status_field, dict):
        raw_status_name = status_field.get("name")
        if isinstance(raw_status_name, str):
            normalized_status_name = raw_status_name.strip()
            if normalized_status_name:
                status_name = normalized_status_name

        status_category = status_field.get("statusCategory")
        if isinstance(status_category, dict):
            raw_status_category_key = status_category.get("key")
            if isinstance(raw_status_category_key, str):
                normalized_status_category_key = raw_status_category_key.strip().lower()
                if normalized_status_category_key:
                    status_category_key = normalized_status_category_key

    normalized_labels = [str(label) for label in labels]
    return issue_key, normalized_labels, status_name, status_category_key, summary, description


def _extract_status_transition(payload: dict) -> tuple[str | None, str | None]:
    changelog = payload.get("changelog")
    if not isinstance(changelog, dict):
        return None, None

    items = changelog.get("items")
    if not isinstance(items, list):
        return None, None

    for item in items:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        if not isinstance(field, str) or field.strip().lower() != "status":
            continue

        from_status = item.get("fromString")
        to_status = item.get("toString")
        normalized_from_status = from_status.strip() if isinstance(from_status, str) and from_status.strip() else None
        normalized_to_status = to_status.strip() if isinstance(to_status, str) and to_status.strip() else None
        return normalized_from_status, normalized_to_status

    return None, None
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


def _validate_webhook_auth(
    tenant: Tenant,
    request: Request,
    request_id: str,
    *,
    session: Session,
    settings,
) -> None:  # noqa: ANN001
    webhook_secret_ref = tenant.jira_config.get("webhook_secret_ref")
    if not webhook_secret_ref:
        return

    expected_token = resolve_secret_ref(
        session,
        secret_ref=str(webhook_secret_ref),
        encryption_key=settings.secrets_encryption_key,
    )
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


def _resolve_global_github_webhook_secret(
    *,
    request_id: str,
    session: Session,
    settings,
) -> str | None:  # noqa: ANN001
    secret_value = resolve_secret_ref(
        session,
        secret_ref=GLOBAL_GITHUB_WEBHOOK_SECRET_REF,
        encryption_key=settings.secrets_encryption_key,
    )
    if not secret_value:
        return None
    return secret_value


def _resolve_tenant_github_webhook_secret(
    *,
    tenant: Tenant,
    request_id: str,
    session: Session,
    settings,
) -> str | None:  # noqa: ANN001
    webhook_secret_ref = tenant.github_config.get("webhook_secret_ref")
    if not webhook_secret_ref:
        return None

    secret_value = resolve_secret_ref(
        session,
        secret_ref=str(webhook_secret_ref),
        encryption_key=settings.secrets_encryption_key,
    )
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
    settings = get_settings()
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

    _validate_webhook_auth(
        tenant=tenant,
        request=request,
        request_id=request_id,
        session=session,
        settings=settings,
    )

    payload, _ = await _read_json_payload(request, request_id=request_id, source="jira")

    issue_key, labels, issue_status, issue_status_category_key, issue_summary, issue_description = (
        _extract_issue_payload(payload)
    )
    delivery_id = _extract_delivery_id(request)
    logger.info(
        "jira_webhook_issue_parsed request_id=%s tenant_id=%s issue_key=%s delivery_id=%s",
        request_id,
        tenant_id,
        issue_key,
        delivery_id,
    )

    configured_ready_statuses = tenant.jira_config.get("ready_statuses")
    if isinstance(configured_ready_statuses, list):
        ready_statuses = [str(status).strip() for status in configured_ready_statuses if str(status).strip()]
    else:
        ready_statuses = []
    if not ready_statuses:
        ready_statuses = ["Ready for Agent"]

    if issue_status is None:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=issue_status_missing",
            request_id,
            tenant_id,
            issue_key,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "issue_key": issue_key,
            "enqueued": False,
            "reason": "issue_status_missing",
        }

    if issue_status_category_key == "done":
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=issue_done issue_status=%s",
            request_id,
            tenant_id,
            issue_key,
            issue_status,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "issue_key": issue_key,
            "enqueued": False,
            "reason": "issue_done",
            "issue_status": issue_status,
        }

    normalized_ready_statuses = {status.casefold() for status in ready_statuses}
    if issue_status.casefold() not in normalized_ready_statuses:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=status_not_ready issue_status=%s",
            request_id,
            tenant_id,
            issue_key,
            issue_status,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "issue_key": issue_key,
            "enqueued": False,
            "reason": "status_not_ready",
            "issue_status": issue_status,
            "ready_statuses": ready_statuses,
            "guidance": format_discord_ready_gate_guidance(
                issue_key=issue_key,
                issue_status=issue_status,
                ready_statuses=ready_statuses,
            ),
        }

    from_status, to_status = _extract_status_transition(payload)
    trigger_reason = "ready_status_recheck"
    if (
        to_status is not None
        and to_status.casefold() in normalized_ready_statuses
        and from_status is not None
        and from_status.casefold() != to_status.casefold()
    ):
        trigger_reason = "status_transition_to_ready"
    logger.info(
        "jira_webhook_ready_trigger request_id=%s tenant_id=%s issue_key=%s trigger_reason=%s issue_status=%s from_status=%s to_status=%s",
        request_id,
        tenant_id,
        issue_key,
        trigger_reason,
        issue_status,
        from_status,
        to_status,
    )

    enqueue_result = enqueue_run(
        session,
        tenant_id=tenant_id,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
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
            "trigger_reason": trigger_reason,
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
        "trigger_reason": trigger_reason,
    }


@router.post("/github/webhook")
async def ingest_github_webhook(
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    settings = get_settings()
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
