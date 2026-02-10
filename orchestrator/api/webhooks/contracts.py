from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.jira_oauth_connection_service import tenant_jira_oauth_context
from orchestrator.api.webhook_payload_utils import extract_webhook_token
from orchestrator.core.project_routing import (
    find_active_project_for_issue_key,
    find_active_project_for_repo_full_name,
)
from orchestrator.core.secret_manager import resolve_scoped_secret_ref
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.jira_oauth import JiraOAuthError

logger = logging.getLogger(__name__)

GLOBAL_GITHUB_WEBHOOK_SECRET_REF = "GITHUB_WEBHOOK_SECRET"
SUPPORTED_JIRA_COMMENT_COMMANDS = {"run", "retry", "ask"}
JIRA_COMMENT_EVENTS = {"comment_created", "comment_updated"}


def normalize_jira_webhook_event(raw_value: object) -> str | None:
    if not isinstance(raw_value, str):
        return None
    normalized = raw_value.strip().lower()
    if not normalized:
        return None
    if normalized.startswith("jira:"):
        normalized = normalized[len("jira:") :]
    return normalized


def adf_to_text(node: object) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(part for part in (adf_to_text(item) for item in node) if part).strip()
    if not isinstance(node, dict):
        return ""

    text = node.get("text")
    if isinstance(text, str):
        return text

    content = node.get("content")
    if isinstance(content, list):
        return " ".join(part for part in (adf_to_text(item) for item in content) if part).strip()
    return ""


def extract_issue_payload(
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
    description_text = adf_to_text(description_raw).strip()
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


def extract_jira_comment_text(payload: dict) -> str | None:
    comment = payload.get("comment")
    if not isinstance(comment, dict):
        return None
    body = comment.get("body")
    if isinstance(body, str):
        text = body.strip()
        return text or None
    if isinstance(body, dict):
        text = adf_to_text(body).strip()
        return text or None
    return None


def parse_jira_comment_command(payload: dict) -> tuple[str | None, str | None, str | None]:
    comment_text = extract_jira_comment_text(payload)
    if not comment_text:
        return None, None, None

    first_non_empty_line = ""
    remaining_lines: list[str] = []
    line_index = -1
    for raw_line in comment_text.splitlines():
        line_index += 1
        candidate = raw_line.strip()
        if candidate:
            first_non_empty_line = candidate
            remaining_lines = [line.strip() for line in comment_text.splitlines()[line_index + 1 :] if line.strip()]
            break
    if not first_non_empty_line:
        return None, None, None
    if not first_non_empty_line.lower().startswith("/mb"):
        return None, None, None

    command_payload = first_non_empty_line[3:].strip()
    if not command_payload:
        return None, None, "invalid_comment_command"

    parts = [part for part in command_payload.split(" ") if part]
    if not parts:
        return None, None, "invalid_comment_command"

    command_name = parts[0].strip().lower()
    if command_name not in SUPPORTED_JIRA_COMMENT_COMMANDS:
        return None, None, "invalid_comment_command"

    if command_name in {"run", "retry"}:
        if len(parts) != 1:
            return None, None, "invalid_comment_command"
        return command_name, None, None

    if command_name == "ask":
        inline_question = " ".join(parts[1:]).strip()
        full_question = "\n".join(part for part in [inline_question, *remaining_lines] if part).strip()
        if not full_question:
            return None, None, "invalid_comment_command"
        return command_name, full_question, None

    return None, None, "invalid_comment_command"


def extract_jira_comment_author_account_id(payload: dict) -> str | None:
    comment = payload.get("comment")
    if not isinstance(comment, dict):
        return None
    author = comment.get("author")
    if not isinstance(author, dict):
        return None
    account_id = author.get("accountId")
    if isinstance(account_id, str) and account_id.strip():
        return account_id.strip()
    return None


def post_jira_comment(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
    comment: str,
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
        return False, str(exc.detail)
    except (JiraOAuthError, ValueError) as exc:
        return False, str(exc)


def extract_status_transition(payload: dict) -> tuple[str | None, str | None]:
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
    webhook_secret_ref = tenant.jira_config.get("webhook_secret_ref")
    if not webhook_secret_ref:
        return

    expected_token = resolve_scoped_secret_ref(
        session,
        secret_ref=str(webhook_secret_ref),
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
) -> None:
    jira_config = dict(tenant.jira_config)
    jira_config["webhook_last_received_at"] = datetime.now(timezone.utc).isoformat()
    jira_config["webhook_last_issue_key"] = issue_key
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
    secret_value = resolve_scoped_secret_ref(
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


def extract_installation_id(payload: dict) -> str | None:
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


def find_tenant_by_installation_id(session: Session, installation_id: str) -> Tenant | None:
    tenants = session.execute(select(Tenant)).scalars().all()
    for tenant in tenants:
        configured_installation_id = str(tenant.github_config.get("installation_id") or "").strip()
        if configured_installation_id and configured_installation_id == installation_id:
            return tenant
    return None


def extract_repository_full_name(payload: dict) -> str | None:
    repository = payload.get("repository")
    if isinstance(repository, dict):
        full_name = repository.get("full_name")
        if isinstance(full_name, str) and full_name.strip():
            return full_name.strip()
    return None


def extract_pull_request_targets(payload: dict) -> list[tuple[int, bool]]:
    targets: list[tuple[int, bool]] = []
    seen: set[int] = set()

    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        number = pull_request.get("number")
        body = pull_request.get("body")
        review_summary_present = isinstance(body, str) and bool(body.strip())
        if isinstance(number, int) and number > 0 and number not in seen:
            targets.append((number, review_summary_present))
            seen.add(number)

    for container_key in ("check_suite", "check_run"):
        container = payload.get(container_key)
        if not isinstance(container, dict):
            continue
        pull_requests = container.get("pull_requests")
        if not isinstance(pull_requests, list):
            continue
        for item in pull_requests:
            if not isinstance(item, dict):
                continue
            number = item.get("number")
            if isinstance(number, int) and number > 0 and number not in seen:
                targets.append((number, True))
                seen.add(number)

    return targets


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
