from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.webhook_payload_utils import (
    extract_webhook_token as _extract_webhook_token,
    read_json_payload as _read_json_payload,
)
from orchestrator.api.discord_ask_context import (
    remove_issue_key_from_tenant_ask_history,
)
from orchestrator.api.dependencies import get_session
from orchestrator.api.jira_oauth_connection_service import tenant_jira_oauth_context
from orchestrator.api.command_entrypoint import (
    execute_tenant_jira_comment_command,
)
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.config import get_settings
from orchestrator.core.project_routing import (
    find_active_project_for_issue_key,
    find_active_project_for_repo_full_name,
)
from orchestrator.core.runs import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    enqueue_run,
)
from orchestrator.core.secret_manager import resolve_scoped_secret_ref
from orchestrator.core.signal_templates import format_discord_ready_gate_guidance
from orchestrator.storage.models import Project, Run, Tenant
from orchestrator.tools.jira_oauth import JiraOAuthError

router = APIRouter(tags=["jira-webhook"])

# Canonical command-ingress entrypoints by source.
execute_jira_comment_command = execute_tenant_jira_comment_command

logger = logging.getLogger(__name__)
GLOBAL_GITHUB_WEBHOOK_SECRET_REF = "GITHUB_WEBHOOK_SECRET"
SUPPORTED_JIRA_COMMENT_COMMANDS = {"run", "retry", "ask"}
JIRA_COMMENT_EVENTS = {"comment_created", "comment_updated"}
ISSUE_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")


@dataclass
class JiraWebhookContext:
    request_id: str
    tenant_id: str
    tenant: Tenant
    payload: dict
    webhook_event: str | None
    issue_key: str
    issue_status: str | None
    issue_status_category_key: str | None
    issue_summary: str | None
    issue_description: str | None
    comment_command: str | None
    comment_command_argument: str | None
    comment_command_error: str | None
    delivery_id: str | None
    project: Project | None


def _normalize_jira_webhook_event(raw_value: object) -> str | None:
    if not isinstance(raw_value, str):
        return None
    normalized = raw_value.strip().lower()
    if not normalized:
        return None
    if normalized.startswith("jira:"):
        normalized = normalized[len("jira:") :]
    return normalized


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


def _extract_jira_comment_text(payload: dict) -> str | None:
    comment = payload.get("comment")
    if not isinstance(comment, dict):
        return None
    body = comment.get("body")
    if isinstance(body, str):
        text = body.strip()
        return text or None
    if isinstance(body, dict):
        text = _adf_to_text(body).strip()
        return text or None
    return None


def _parse_jira_comment_command(payload: dict) -> tuple[str | None, str | None, str | None]:
    comment_text = _extract_jira_comment_text(payload)
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


def _extract_jira_comment_author_account_id(payload: dict) -> str | None:
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


def _post_jira_comment(
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


def _record_jira_webhook_receipt(
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


def _resolve_global_github_webhook_secret(
    *,
    request_id: str,
    session: Session,
    settings,
) -> str | None:  # noqa: ANN001
    secret_value = resolve_scoped_secret_ref(
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


def _extract_repository_full_name(payload: dict) -> str | None:
    repository = payload.get("repository")
    if isinstance(repository, dict):
        full_name = repository.get("full_name")
        if isinstance(full_name, str) and full_name.strip():
            return full_name.strip()
    return None


def _extract_pull_request_targets(payload: dict) -> list[tuple[int, bool]]:
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


def _resolve_active_project_for_issue(*, session: Session, tenant_id: str, issue_key: str) -> Project | None:
    return find_active_project_for_issue_key(
        session,
        tenant_id=tenant_id,
        issue_key=issue_key,
    )


def _resolve_active_project_for_repo(
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


def _jira_webhook_response(
    context: JiraWebhookContext,
    *,
    enqueued: bool,
    reason: str | None,
    **extra: object,
) -> dict:
    payload: dict[str, object] = {
        "request_id": context.request_id,
        "tenant_id": context.tenant_id,
        "project_id": context.project.project_id if context.project is not None else None,
        "issue_key": context.issue_key,
        "enqueued": enqueued,
    }
    if reason is not None:
        payload["reason"] = reason
    payload.update(extra)
    return payload


async def _stage_parse_jira_webhook_context(
    *,
    tenant_id: str,
    tenant: Tenant,
    request: Request,
    request_id: str,
    session: Session,
    settings,  # noqa: ANN001
) -> JiraWebhookContext:
    _validate_webhook_auth(
        tenant=tenant,
        request=request,
        request_id=request_id,
        session=session,
        settings=settings,
    )

    payload, _ = await _read_json_payload(request, request_id=request_id, source="jira")
    webhook_event = _normalize_jira_webhook_event(payload.get("webhookEvent"))

    issue_key, _labels, issue_status, issue_status_category_key, issue_summary, issue_description = (
        _extract_issue_payload(payload)
    )
    comment_command, comment_command_argument, comment_command_error = _parse_jira_comment_command(payload)
    delivery_id = _extract_delivery_id(request)
    _record_jira_webhook_receipt(
        session=session,
        tenant=tenant,
        delivery_id=delivery_id,
        issue_key=issue_key,
    )
    logger.info(
        "jira_webhook_issue_parsed request_id=%s tenant_id=%s issue_key=%s delivery_id=%s webhook_event=%s comment_command=%s comment_command_error=%s",
        request_id,
        tenant_id,
        issue_key,
        delivery_id,
        webhook_event,
        comment_command,
        comment_command_error,
    )
    project = _resolve_active_project_for_issue(
        session=session,
        tenant_id=tenant_id,
        issue_key=issue_key,
    )
    return JiraWebhookContext(
        request_id=request_id,
        tenant_id=tenant_id,
        tenant=tenant,
        payload=payload,
        webhook_event=webhook_event,
        issue_key=issue_key,
        issue_status=issue_status,
        issue_status_category_key=issue_status_category_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        comment_command=comment_command,
        comment_command_argument=comment_command_argument,
        comment_command_error=comment_command_error,
        delivery_id=delivery_id,
        project=project,
    )


def _stage_handle_issue_deleted(
    *,
    context: JiraWebhookContext,
    session: Session,
) -> dict | None:
    if context.webhook_event != "issue_deleted":
        return None

    removed_entries = remove_issue_key_from_tenant_ask_history(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
    )
    logger.info(
        "jira_webhook_issue_deleted request_id=%s tenant_id=%s issue_key=%s removed_history_entries=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        removed_entries,
    )
    return _jira_webhook_response(
        context,
        enqueued=False,
        reason="issue_deleted",
        removed_history_entries=removed_entries,
        webhook_event=context.webhook_event,
    )


def _stage_handle_invalid_comment_command(
    *,
    context: JiraWebhookContext,
) -> dict | None:
    if not context.comment_command_error:
        return None
    logger.info(
        "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=invalid_comment_command",
        context.request_id,
        context.tenant_id,
        context.issue_key,
    )
    return _jira_webhook_response(
        context,
        enqueued=False,
        reason="invalid_comment_command",
        webhook_event=context.webhook_event,
    )


def _stage_handle_comment_event_memory(
    *,
    context: JiraWebhookContext,
    session: Session,
) -> int:
    if context.webhook_event not in JIRA_COMMENT_EVENTS:
        return 0
    removed_entries = remove_issue_key_from_tenant_ask_history(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
    )
    logger.info(
        "jira_webhook_comment_event_memory_cleared request_id=%s tenant_id=%s issue_key=%s webhook_event=%s removed_history_entries=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        context.webhook_event,
        removed_entries,
    )
    return removed_entries


def _stage_handle_comment_without_command(
    *,
    context: JiraWebhookContext,
    removed_history_entries: int,
) -> dict | None:
    if context.webhook_event not in JIRA_COMMENT_EVENTS or context.comment_command is not None:
        return None
    logger.info(
        "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=comment_without_command webhook_event=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        context.webhook_event,
    )
    return _jira_webhook_response(
        context,
        enqueued=False,
        reason="comment_without_command",
        webhook_event=context.webhook_event,
        removed_history_entries=removed_history_entries,
    )


def _stage_handle_comment_ask_command(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> dict | None:
    if context.comment_command != "ask":
        return None

    question = (context.comment_command_argument or "").strip()
    if not question:
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason="invalid_comment_command",
        )

    author_account_id = _extract_jira_comment_author_account_id(context.payload) or "jira-user"
    try:
        ask_response = execute_jira_comment_command(
            session=session,
            tenant_id=context.tenant_id,
            payload=DiscordCommandRequest(
                user_id=author_account_id,
                channel_id=None,
                command=f"!ask @{context.issue_key} {question}",
            ),
        )
        response_text = ask_response.message.strip()
        if not response_text:
            response_text = "I processed your question but returned no response text."
    except HTTPException as exc:
        response_text = f"Unable to process `/mb ask`: {exc.detail}"

    posted, post_error = _post_jira_comment(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
        comment=response_text,
        settings=settings,
    )
    return _jira_webhook_response(
        context,
        enqueued=False,
        reason="comment_command_ask",
        command=context.comment_command,
        question=question,
        comment_posted=posted,
        comment_error=post_error,
        webhook_event=context.webhook_event,
    )


def _resolve_ready_statuses_for_tenant(tenant: Tenant) -> list[str]:
    configured_ready_statuses = tenant.jira_config.get("ready_statuses")
    if isinstance(configured_ready_statuses, list):
        ready_statuses = [str(status).strip() for status in configured_ready_statuses if str(status).strip()]
    else:
        ready_statuses = []
    return ready_statuses or ["Ready for Agent"]


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

    context = await _stage_parse_jira_webhook_context(
        tenant_id=tenant_id,
        tenant=tenant,
        request=request,
        request_id=request_id,
        session=session,
        settings=settings,
    )

    deleted_response = _stage_handle_issue_deleted(context=context, session=session)
    if deleted_response is not None:
        return deleted_response

    invalid_comment_response = _stage_handle_invalid_comment_command(context=context)
    if invalid_comment_response is not None:
        return invalid_comment_response

    removed_history_entries = _stage_handle_comment_event_memory(context=context, session=session)
    comment_without_command_response = _stage_handle_comment_without_command(
        context=context,
        removed_history_entries=removed_history_entries,
    )
    if comment_without_command_response is not None:
        return comment_without_command_response

    comment_ask_response = _stage_handle_comment_ask_command(
        context=context,
        session=session,
        settings=settings,
    )
    if comment_ask_response is not None:
        return comment_ask_response

    ready_statuses = _resolve_ready_statuses_for_tenant(tenant)
    if context.issue_status is None:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=issue_status_missing",
            request_id,
            tenant_id,
            context.issue_key,
        )
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason="issue_status_missing",
            webhook_event=context.webhook_event,
        )

    if context.issue_status_category_key == "done":
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=issue_done issue_status=%s",
            request_id,
            tenant_id,
            context.issue_key,
            context.issue_status,
        )
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason="issue_done",
            issue_status=context.issue_status,
            webhook_event=context.webhook_event,
        )

    normalized_ready_statuses = {status.casefold() for status in ready_statuses}
    if context.issue_status.casefold() not in normalized_ready_statuses:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=status_not_ready issue_status=%s",
            request_id,
            tenant_id,
            context.issue_key,
            context.issue_status,
        )
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason="status_not_ready",
            issue_status=context.issue_status,
            ready_statuses=ready_statuses,
            guidance=format_discord_ready_gate_guidance(
                issue_key=context.issue_key,
                issue_status=context.issue_status,
                ready_statuses=ready_statuses,
            ),
            webhook_event=context.webhook_event,
        )

    if context.project is None:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=project_not_mapped",
            request_id,
            tenant_id,
            context.issue_key,
        )
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason="project_not_mapped",
            command=context.comment_command,
            webhook_event=context.webhook_event,
        )

    from_status, to_status = _extract_status_transition(context.payload)
    trigger_reason = "ready_status_recheck"
    if context.comment_command == "run":
        trigger_reason = "comment_command_run"
    elif context.comment_command == "retry":
        trigger_reason = "comment_command_retry"
    elif (
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
        context.issue_key,
        trigger_reason,
        context.issue_status,
        from_status,
        to_status,
    )

    retry_source_run = None
    resolved_issue_description = context.issue_description
    if context.comment_command == "retry":
        retryable_statuses = {RUN_STATUS_FAILED, RUN_STATUS_BLOCKED, RUN_STATUS_CANCELLED}
        retry_source_run = session.execute(
            select(Run)
            .where(
                Run.tenant_id == tenant_id,
                Run.issue_key == context.issue_key,
                Run.status.in_(retryable_statuses),
            )
            .order_by(Run.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if retry_source_run is None:
            logger.info(
                "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=no_retryable_run",
                request_id,
                tenant_id,
                context.issue_key,
            )
            return _jira_webhook_response(
                context,
                enqueued=False,
                reason="no_retryable_run",
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
            )
        resolved_issue_description = retry_source_run.issue_description

    enqueue_result = enqueue_run(
        session,
        tenant_id=tenant_id,
        project_id=context.project.project_id,
        issue_key=context.issue_key,
        issue_summary=context.issue_summary,
        issue_description=resolved_issue_description,
        repo_url=context.project.github_repository,
        delivery_id=context.delivery_id,
        max_concurrent_runs=tenant.policy_config.get("max_concurrent_runs"),
    )
    if not enqueue_result.enqueued:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=%s run_id=%s",
            request_id,
            tenant_id,
            context.issue_key,
            enqueue_result.reason,
            enqueue_result.run.run_id,
        )
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason=enqueue_result.reason,
            run_id=enqueue_result.run.run_id,
            trigger_reason=trigger_reason,
            command=context.comment_command,
            webhook_event=context.webhook_event,
        )
    logger.info(
        "jira_webhook_enqueued request_id=%s tenant_id=%s issue_key=%s run_id=%s",
        request_id,
        tenant_id,
        context.issue_key,
        enqueue_result.run.run_id,
    )

    return _jira_webhook_response(
        context,
        enqueued=True,
        reason=None,
        run_id=enqueue_result.run.run_id,
        trigger_reason=trigger_reason,
        command=context.comment_command,
        webhook_event=context.webhook_event,
    )
