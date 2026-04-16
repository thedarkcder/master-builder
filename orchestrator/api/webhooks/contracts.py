from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestrator.api.jira_oauth.connection_service import tenant_jira_oauth_context
from orchestrator.api.webhooks.github_payload_contracts import (
    extract_installation_id,
    extract_pull_request_targets,
    extract_repository_full_name,
    find_tenant_by_installation_id,
)
from orchestrator.api.webhooks.jira_payload_contracts import (
    adf_to_text,
    extract_changed_fields,
    extract_issue_payload,
    extract_jira_comment_author_account_id,
    extract_jira_comment_text,
    extract_status_transition,
    normalize_jira_webhook_event,
    parse_jira_comment_command,
)
from orchestrator.api.webhooks.payload_utils import extract_webhook_token
from orchestrator.core.project_routing import (
    find_active_project_for_issue_key,
    find_active_project_for_repo_full_name,
)
from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.core.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.core.decision_types import tenant_jira_webhook_secret_ref
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.jira_oauth import JiraOAuthError

logger = logging.getLogger(__name__)

GLOBAL_GITHUB_WEBHOOK_SECRET_REF = "GITHUB_WEBHOOK_SECRET"
JIRA_COMMENT_EVENTS = {"comment_created", "comment_updated"}

# Explicit public contract surface consumed by ingress routes/services.
__all__ = [
    "JIRA_COMMENT_EVENTS",
    "adf_to_text",
    "extract_changed_fields",
    "extract_delivery_id",
    "extract_installation_id",
    "extract_issue_payload",
    "extract_jira_comment_author_account_id",
    "extract_jira_comment_text",
    "extract_pull_request_targets",
    "extract_repository_full_name",
    "extract_status_transition",
    "find_tenant_by_installation_id",
    "normalize_jira_webhook_event",
    "parse_jira_comment_command",
    "post_jira_comment",
    "record_jira_webhook_receipt",
    "resolve_active_project_for_issue",
    "resolve_active_project_for_repo",
    "resolve_global_github_webhook_secret",
    "resolve_tenant_github_webhook_secret",
    "validate_github_webhook_signature",
    "validate_webhook_auth",
]


def post_jira_comment(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
    comment: str | dict,
    settings,  # noqa: ANN001
) -> tuple[bool, str | None]:
    try:
        oauth = tenant_jira_oauth_context(session=session, tenant=tenant, settings=settings)
        oauth.client.add_issue_comment(
            access_token=oauth.access_token,
            cloud_id=oauth.connection.cloud_id,
            issue_id_or_key=issue_key,
            comment=comment,
        )
        return True, None
    except HTTPException as exc:
        logger.exception(
            "jira_comment_post_failed_http tenant_id=%s issue_key=%s detail=%s error=%s",
            tenant.tenant_id,
            issue_key,
            exc.detail,
            exc,
        )
        return False, str(exc.detail)
    except (JiraOAuthError, ValueError) as exc:
        logger.exception(
            "jira_comment_post_failed tenant_id=%s issue_key=%s error=%s",
            tenant.tenant_id,
            issue_key,
            exc,
        )
        return False, str(exc)


def extract_delivery_id(request: Request) -> str | None:
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


def validate_webhook_auth(
    tenant: Tenant,
    request: Request,
    request_id: str,
    *,
    session: Session,
    settings,
) -> None:  # noqa: ANN001
    webhook_secret_ref = tenant_jira_webhook_secret_ref(tenant)
    if not webhook_secret_ref:
        return

    expected_token = resolve_scoped_secret_ref(
        session,
        secret_ref=webhook_secret_ref,
        encryption_key=settings.secrets_encryption_key,
        tenant_id=tenant.tenant_id,
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

    presented_token = extract_webhook_token(request)
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


def record_jira_webhook_receipt(
    *,
    session: Session,
    tenant: Tenant,
    delivery_id: str | None,
    issue_key: str,
    webhook_event: str | None = None,
) -> None:
    jira_config = dict(tenant.jira_config)
    jira_config["webhook_last_received_at"] = datetime.now(timezone.utc).isoformat()
    jira_config["webhook_last_issue_key"] = issue_key
    if webhook_event:
        jira_config["webhook_last_event"] = str(webhook_event).strip().lower()
    if delivery_id:
        jira_config["webhook_last_delivery_id"] = delivery_id
    tenant.jira_config = jira_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()


def resolve_global_github_webhook_secret(
    *,
    request_id: str,
    session: Session,
    settings,
) -> str | None:  # noqa: ANN001
    del request_id
    secret_value = resolve_platform_secret_ref(
        session,
        secret_ref=GLOBAL_GITHUB_WEBHOOK_SECRET_REF,
        encryption_key=settings.secrets_encryption_key,
    )
    if not secret_value:
        return None
    return secret_value


def resolve_tenant_github_webhook_secret(
    *,
    tenant: Tenant,
    request_id: str,
    session: Session,
    settings,
) -> str | None:  # noqa: ANN001
    webhook_secret_ref = tenant.github_config.get("webhook_secret_ref")
    if not webhook_secret_ref:
        return None

    secret_value = resolve_scoped_secret_ref(
        session,
        secret_ref=str(webhook_secret_ref),
        encryption_key=settings.secrets_encryption_key,
        tenant_id=tenant.tenant_id,
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


def validate_github_webhook_signature(
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


def resolve_active_project_for_issue(*, session: Session, tenant_id: str, issue_key: str) -> Project | None:
    return find_active_project_for_issue_key(
        session,
        tenant_id=tenant_id,
        issue_key=issue_key,
    )


def resolve_active_project_for_repo(
    *,
    session: Session,
    tenant_id: str,
    repo_full_name: str,
) -> Project | None:
    return find_active_project_for_repo_full_name(
        session,
        tenant_id=tenant_id,
        repo_full_name=repo_full_name,
    )
