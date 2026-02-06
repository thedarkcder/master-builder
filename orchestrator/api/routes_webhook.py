from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.storage.models import Run, Tenant

router = APIRouter(tags=["jira-webhook"])

ACTIVE_RUN_STATUSES = {"queued", "running"}
logger = logging.getLogger(__name__)


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

    payload = await request.json()
    if not isinstance(payload, dict):
        logger.warning(
            "jira_webhook_invalid_payload request_id=%s tenant_id=%s",
            request_id,
            tenant_id,
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload")

    issue_key, labels = _extract_issue_payload(payload)
    logger.info(
        "jira_webhook_issue_parsed request_id=%s tenant_id=%s issue_key=%s",
        request_id,
        tenant_id,
        issue_key,
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

    existing_active_run = session.execute(
        select(Run).where(
            Run.tenant_id == tenant_id,
            Run.issue_key == issue_key,
            Run.status.in_(ACTIVE_RUN_STATUSES),
        )
    ).scalar_one_or_none()

    if existing_active_run is not None:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=run_already_active run_id=%s",
            request_id,
            tenant_id,
            issue_key,
            existing_active_run.run_id,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "issue_key": issue_key,
            "enqueued": False,
            "reason": "run_already_active",
            "run_id": existing_active_run.run_id,
        }

    run = Run(
        run_id=str(uuid4()),
        tenant_id=tenant_id,
        issue_key=issue_key,
        repo_url=None,
        branch=None,
        pr_url=None,
        status="queued",
        last_error=None,
        plan=None,
        created_at=datetime.now(timezone.utc),
        started_at=None,
        finished_at=None,
    )
    session.add(run)
    session.commit()
    logger.info(
        "jira_webhook_enqueued request_id=%s tenant_id=%s issue_key=%s run_id=%s",
        request_id,
        tenant_id,
        issue_key,
        run.run_id,
    )

    return {
        "request_id": request_id,
        "tenant_id": tenant_id,
        "issue_key": issue_key,
        "enqueued": True,
        "run_id": run.run_id,
    }
