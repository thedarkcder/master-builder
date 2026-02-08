from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.routes_discord import (
    _project_filter_jql,
    _search_jira_issues_for_tenant,
    execute_discord_command,
)
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.config import get_settings
from orchestrator.core.discord_notifications import send_tenant_discord_message
from orchestrator.core.reviewer import ReviewAgentGate
from orchestrator.core.runs import enqueue_run
from orchestrator.core.secret_manager import resolve_secret_ref
from orchestrator.core.signal_templates import format_discord_ready_gate_guidance
from orchestrator.storage.models import Tenant
from orchestrator.tools.github_app import GitHubApiError, github_client_from_tenant_config

router = APIRouter(tags=["jira-webhook"])

logger = logging.getLogger(__name__)
DEFAULT_WEBHOOK_MAX_BODY_BYTES = 1_048_576
HTTP_413_TOO_LARGE = getattr(
    status,
    "HTTP_413_CONTENT_TOO_LARGE",
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
)
GLOBAL_GITHUB_WEBHOOK_SECRET_REF = "GITHUB_WEBHOOK_SECRET"
DISCORD_INTERACTIONS_PUBLIC_KEY_SECRET_REF = "DISCORD_INTERACTIONS_PUBLIC_KEY"


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


def _resolve_discord_interactions_public_key(
    *,
    session: Session,
    settings,
) -> bytes:  # noqa: ANN001
    raw_public_key = resolve_secret_ref(
        session,
        secret_ref=DISCORD_INTERACTIONS_PUBLIC_KEY_SECRET_REF,
        encryption_key=settings.secrets_encryption_key,
    ).strip()
    if not raw_public_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Discord interactions public key is missing. "
                f"Set managed secret '{DISCORD_INTERACTIONS_PUBLIC_KEY_SECRET_REF}'."
            ),
        )

    try:
        public_key_bytes = bytes.fromhex(raw_public_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Discord interactions public key must be a hex-encoded Ed25519 public key",
        ) from exc

    if len(public_key_bytes) != 32:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Discord interactions public key must be 32 bytes (64 hex chars)",
        )
    return public_key_bytes


def _validate_discord_interaction_signature(
    *,
    request: Request,
    payload_bytes: bytes,
    public_key: bytes,
) -> None:
    signature_hex = (request.headers.get("X-Signature-Ed25519") or "").strip()
    timestamp = (request.headers.get("X-Signature-Timestamp") or "").strip()
    if not signature_hex or not timestamp:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Discord interaction signature headers",
        )

    try:
        signature = bytes.fromhex(signature_hex)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Discord interaction signature",
        ) from exc

    message = timestamp.encode("utf-8") + payload_bytes
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except InvalidSignature as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Discord interaction signature",
        ) from exc


def _discord_interaction_response(*, content: str, ephemeral: bool = True) -> JSONResponse:
    response_data = {"content": content}
    if ephemeral:
        response_data["flags"] = 64
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"type": 4, "data": response_data},
    )


def _discord_autocomplete_response(*, choices: list[dict]) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"type": 8, "data": {"choices": choices[:25]}},
    )


def _find_tenant_for_discord_channel(
    *,
    session: Session,
    channel_id: str,
) -> Tenant | None:
    tenants = session.execute(select(Tenant).where(Tenant.is_enabled.is_(True))).scalars().all()
    matches: list[Tenant] = []
    for tenant in tenants:
        discord_channel_id = str((tenant.discord_config or {}).get("channel_id") or "").strip()
        if discord_channel_id and discord_channel_id == channel_id:
            matches.append(tenant)
    if len(matches) != 1:
        return None
    return matches[0]


def _flatten_discord_option_values(options: object) -> list[str]:
    if not isinstance(options, list):
        return []
    flattened: list[str] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        option_type = option.get("type")
        if option_type in {1, 2}:  # SUB_COMMAND / SUB_COMMAND_GROUP
            nested_name = option.get("name")
            if isinstance(nested_name, str) and nested_name.strip():
                flattened.append(nested_name.strip())
            flattened.extend(_flatten_discord_option_values(option.get("options")))
            continue
        value = option.get("value")
        if value is None:
            continue
        flattened.append(str(value))
    return flattened


def _find_focused_discord_option(options: object) -> tuple[str, str] | None:
    if not isinstance(options, list):
        return None
    for option in options:
        if not isinstance(option, dict):
            continue
        option_type = option.get("type")
        if option_type in {1, 2}:  # SUB_COMMAND / SUB_COMMAND_GROUP
            focused = _find_focused_discord_option(option.get("options"))
            if focused is not None:
                return focused
            continue
        if option.get("focused") is True:
            name = str(option.get("name") or "").strip()
            value = str(option.get("value") or "").strip()
            if name:
                return name, value
    return None


def _discord_option_value(options: object, *, name: str) -> str | None:
    if not isinstance(options, list):
        return None
    for option in options:
        if not isinstance(option, dict):
            continue
        option_type = option.get("type")
        if option_type in {1, 2}:  # SUB_COMMAND / SUB_COMMAND_GROUP
            nested = _discord_option_value(option.get("options"), name=name)
            if nested is not None:
                return nested
            continue
        option_name = str(option.get("name") or "").strip()
        if option_name != name:
            continue
        value = option.get("value")
        if value is None:
            return None
        return str(value).strip()
    return None


def _discord_issue_autocomplete_choices(
    *,
    session: Session,
    tenant: Tenant,
    current_value: str,
) -> list[dict]:
    project_jql = _project_filter_jql(tenant)
    normalized = current_value.strip().upper()
    if normalized:
        jql = f'{project_jql} AND key ~ "{normalized}*" ORDER BY updated DESC'
    else:
        jql = f"{project_jql} ORDER BY updated DESC"
    issues = _search_jira_issues_for_tenant(
        session=session,
        tenant=tenant,
        jql=jql,
        max_results=25,
    )

    choices: list[dict] = []
    seen_keys: set[str] = set()
    for issue in issues:
        if issue.key in seen_keys:
            continue
        seen_keys.add(issue.key)
        summary = (issue.summary or "").strip()
        display = f"{issue.key} — {summary}" if summary else issue.key
        choices.append({"name": display[:100], "value": issue.key[:100]})
    return choices


def _parse_discord_interaction_command(payload: dict) -> tuple[str, str, str]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing interaction data")

    command_name = data.get("name")
    if not isinstance(command_name, str) or not command_name.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing interaction command name")

    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing interaction channel_id")

    user_id: str | None = None
    member = payload.get("member")
    if isinstance(member, dict):
        member_user = member.get("user")
        if isinstance(member_user, dict):
            raw_user_id = member_user.get("id")
            if isinstance(raw_user_id, str) and raw_user_id.strip():
                user_id = raw_user_id.strip()
    if user_id is None:
        direct_user = payload.get("user")
        if isinstance(direct_user, dict):
            raw_user_id = direct_user.get("id")
            if isinstance(raw_user_id, str) and raw_user_id.strip():
                user_id = raw_user_id.strip()
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing interaction user_id")

    normalized_command = command_name.strip().lower()
    options = data.get("options")
    command_text = f"!{normalized_command}"
    if normalized_command == "ask":
        issue_key = _discord_option_value(options, name="issue_key")
        question = _discord_option_value(options, name="question")
        if issue_key:
            command_text = f"{command_text} @{issue_key}"
        if question:
            command_text = f"{command_text} {question}"
    elif normalized_command == "run":
        issue_key = _discord_option_value(options, name="issue_key")
        if issue_key:
            command_text = f"{command_text} {issue_key}"
    elif normalized_command == "link":
        issue_key = _discord_option_value(options, name="issue_key")
        if issue_key:
            command_text = f"{command_text} {issue_key}"
    elif normalized_command == "retry":
        target = _discord_option_value(options, name="target")
        if target:
            command_text = f"{command_text} {target}"
    elif normalized_command == "issues":
        subcommands = options if isinstance(options, list) else []
        for option in subcommands:
            if not isinstance(option, dict):
                continue
            if option.get("type") != 1:
                continue
            subcommand_name = str(option.get("name") or "").strip().lower()
            if not subcommand_name:
                continue
            command_text = f"{command_text} {subcommand_name}"
            spec = _discord_option_value(option.get("options"), name="spec")
            if spec:
                command_text = f"{command_text} {spec}"
            break
    else:
        option_values = _flatten_discord_option_values(options)
        if option_values:
            command_text = f"{command_text} {' '.join(option_values)}"

    return user_id, channel_id.strip(), command_text


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


@router.post("/discord/interactions")
async def ingest_discord_interaction(
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    settings = get_settings()
    request_id = request.headers.get("X-Request-Id") or str(uuid4())

    payload, payload_bytes = await _read_json_payload(request, request_id=request_id, source="discord")
    public_key = _resolve_discord_interactions_public_key(session=session, settings=settings)
    _validate_discord_interaction_signature(
        request=request,
        payload_bytes=payload_bytes,
        public_key=public_key,
    )

    interaction_type = payload.get("type")
    if interaction_type == 1:  # PING
        return JSONResponse(status_code=status.HTTP_200_OK, content={"type": 1})

    if interaction_type == 4:  # APPLICATION_COMMAND_AUTOCOMPLETE
        channel_id = payload.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id.strip():
            return _discord_autocomplete_response(choices=[])
        tenant = _find_tenant_for_discord_channel(session=session, channel_id=channel_id.strip())
        if tenant is None:
            return _discord_autocomplete_response(choices=[])

        data = payload.get("data")
        if not isinstance(data, dict):
            return _discord_autocomplete_response(choices=[])
        command_name = str(data.get("name") or "").strip().lower()
        focused = _find_focused_discord_option(data.get("options"))
        if focused is None:
            return _discord_autocomplete_response(choices=[])
        focused_name, focused_value = focused
        supports_issue_autocomplete = (
            (command_name in {"run", "link"} and focused_name == "issue_key")
            or (command_name in {"retry"} and focused_name == "target")
            or (command_name in {"ask"} and focused_name == "issue_key")
        )
        if not supports_issue_autocomplete:
            return _discord_autocomplete_response(choices=[])
        try:
            choices = _discord_issue_autocomplete_choices(
                session=session,
                tenant=tenant,
                current_value=focused_value,
            )
        except HTTPException:
            choices = []
        return _discord_autocomplete_response(choices=choices)

    if interaction_type != 2:  # APPLICATION_COMMAND
        return _discord_interaction_response(
            content=f"Unsupported Discord interaction type '{interaction_type}'",
            ephemeral=True,
        )

    try:
        user_id, channel_id, command_text = _parse_discord_interaction_command(payload)
    except HTTPException as exc:
        return _discord_interaction_response(content=str(exc.detail), ephemeral=True)

    tenant = _find_tenant_for_discord_channel(session=session, channel_id=channel_id)
    if tenant is None:
        return _discord_interaction_response(
            content="No enabled tenant is configured for this Discord channel.",
            ephemeral=True,
        )

    try:
        command_response = execute_discord_command(
            tenant_id=tenant.tenant_id,
            payload=DiscordCommandRequest(
                user_id=user_id,
                command=command_text,
                channel_id=channel_id,
            ),
            session=session,
        )
    except HTTPException as exc:
        return _discord_interaction_response(content=str(exc.detail), ephemeral=True)

    return _discord_interaction_response(content=command_response.message, ephemeral=True)


@router.post("/discord/webhook/{tenant_id}")
async def ingest_discord_webhook(
    tenant_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    settings = get_settings()
    request_id = request.headers.get("X-Request-Id") or str(uuid4())
    logger.info("discord_webhook_received request_id=%s tenant_id=%s", request_id, tenant_id)

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tenant")
    if not tenant.is_enabled:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "request_id": request_id,
                "tenant_id": tenant_id,
                "accepted": False,
                "reason": "tenant_disabled",
            },
        )

    discord_config = tenant.discord_config or {}
    command_secret_ref = str(discord_config.get("command_secret_ref") or "").strip()
    if command_secret_ref:
        presented_token = _extract_webhook_token(request)
        expected_token = resolve_secret_ref(
            session,
            secret_ref=command_secret_ref,
            encryption_key=settings.secrets_encryption_key,
        )
        if not expected_token:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Discord command authentication is misconfigured",
            )
        if not presented_token or not secrets.compare_digest(presented_token, expected_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Discord webhook token",
            )

    payload, _ = await _read_json_payload(request, request_id=request_id, source="discord")
    user_id = payload.get("user_id")
    command = payload.get("command")
    channel_id = payload.get("channel_id")
    if not isinstance(user_id, str) or not user_id.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing user_id")
    if not isinstance(command, str) or not command.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing command")
    if channel_id is not None and not isinstance(channel_id, str):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid channel_id")

    command_response = execute_discord_command(
        tenant_id=tenant_id,
        payload=DiscordCommandRequest(
            user_id=user_id.strip(),
            command=command.strip(),
            channel_id=channel_id.strip() if isinstance(channel_id, str) and channel_id.strip() else None,
        ),
        session=session,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "request_id": request_id,
            "tenant_id": tenant_id,
            "accepted": True,
            "result": command_response.model_dump(),
        },
    )


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

    def secret_lookup(secret_ref: str) -> str | None:
        return resolve_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
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
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "request_id": request_id,
                    "delivery_id": delivery_id,
                    "tenant_id": tenant.tenant_id,
                    "event": github_event,
                    "action": normalized_action,
                    "accepted": False,
                    "reason": "review_evaluation_failed",
                },
            )

        send_result = send_tenant_discord_message(
            session=session,
            tenant=tenant,
            message=signal.message,
            settings=settings,
            event="review_signal",
        )
        signals.append(
            {
                "pr_number": pr_number,
                "ready": signal.ready,
                "state": signal.state,
                "policy_pack": signal.policy_pack,
                "must_fix_findings": list(signal.must_fix_findings),
                "discord_sent": send_result.sent,
                "discord_reason": send_result.reason,
            }
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
            "reason": "review_processed",
            "signals": signals,
        },
    )
