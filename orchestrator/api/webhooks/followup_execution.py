from __future__ import annotations

import logging
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from orchestrator.api.discord.shared.errors import DiscordInteractionWebhookExpiredError
from orchestrator.api.discord.shared.state import command_matches
from orchestrator.core.communications import (
    DiscordAskWithThreadAction,
    DiscordInteractionFollowupAction,
    DiscordSeedWithThreadAction,
    DiscordThreadReplyAction,
)
from orchestrator.core.communications.integration_contracts import TransportActionExecutor
from orchestrator.core.error_observability import emit_hard_error
from orchestrator.core.observability import reset_log_context, set_log_context
from orchestrator.storage.models import Tenant
from orchestrator.tools.discord_api import DiscordApiError

logger = logging.getLogger(__name__)
ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
ISSUE_KEY_IN_TEXT_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")


def _followup_context_type_for_message(*, command_name: str | None, content: str, issue_key: str | None) -> str:
    normalized_command_name = str(command_name or "").strip().lower()
    normalized_content = str(content or "").strip()
    normalized_issue_key = str(issue_key or "").strip().upper()
    if normalized_issue_key and (
        normalized_command_name == "reply"
        or "Decision Gate still needs clarification for `" in normalized_content
        or "Good To Do still needs clarification for `" in normalized_content
    ):
        return "decision_gate"
    return "ask_thread"


@dataclass(frozen=True)
class DiscordFollowupExecutionDeps:
    session_factory: Callable[[], AbstractContextManager[Session]]
    settings_factory: Callable[[], object]
    execute_command_ingress: Callable[..., object]
    command_request_factory: Callable[..., object]
    build_command_followup_message: Callable[..., str]
    ask_confirmation_components: Callable[[str], list[dict]]
    ask_reply_components: Callable[[], list[dict]]
    transport_executor: TransportActionExecutor
    consume_pending_ask_action: Callable[..., dict | None]


def resolve_issue_key_hint(*, command_text: str, command_params: dict[str, str] | None) -> str | None:
    if isinstance(command_params, dict):
        candidate = str(command_params.get("issue_key") or "").strip().upper()
        if ISSUE_KEY_PATTERN.fullmatch(candidate):
            return candidate
    normalized_command_text = str(command_text or "").strip().upper()
    match = ISSUE_KEY_IN_TEXT_PATTERN.search(normalized_command_text)
    if match is None:
        return None
    candidate = match.group(0)
    if ISSUE_KEY_PATTERN.fullmatch(candidate):
        return candidate
    return None


async def run_discord_command_followup(
    *,
    deps: DiscordFollowupExecutionDeps,
    tenant_id: str,
    user_id: str,
    channel_id: str,
    command_text: str,
    application_id: str,
    interaction_token: str,
    reply_to_message_id: str | None = None,
    command_params: dict[str, str] | None = None,
    attachments: list[dict[str, str]] | None = None,
) -> None:
    correlation_id = reply_to_message_id or uuid4().hex
    context_tokens = None
    settings = None
    content = f"<@{user_id}> Command failed due to an internal error."
    components: list[dict] | None = None
    sent_to_thread = False
    try:
        context_tokens = set_log_context(
            correlation_id=correlation_id,
            tenant_id=tenant_id,
            agent_id=user_id,
        )
        settings = deps.settings_factory()
        try:
            with deps.session_factory() as session:
                tenant = session.get(Tenant, tenant_id)
                if tenant is None or not tenant.is_enabled:
                    content = f"<@{user_id}> Command failed: tenant is unavailable."
                else:
                    initial_thread_attempted = False
                    issue_key: str | None = resolve_issue_key_hint(
                        command_text=command_text,
                        command_params=command_params,
                    )
                    try:
                        command_response = deps.execute_command_ingress(
                            tenant_id=tenant_id,
                            payload=deps.command_request_factory(
                                user_id=user_id,
                                command=command_text,
                                channel_id=channel_id,
                                command_params=command_params,
                                attachments=attachments or [],
                            ),
                            session=session,
                            defer_seed_issues=False,
                            ingress_source="discord",
                        )
                        data = command_response.data if isinstance(command_response.data, dict) else {}
                        requires_confirmation = bool(data.get("requires_confirmation")) and command_response.command == "ask"
                        if requires_confirmation:
                            request_id = str(data.get("request_id") or "").strip()
                            proposed_command = str(data.get("proposed_command") or "").strip()
                            summary = str(data.get("summary") or command_response.message or "").strip()
                            if request_id and proposed_command:
                                content = "\n".join(
                                    [
                                        f"<@{user_id}> {summary}",
                                        "",
                                        f"Proposed action: `{proposed_command}`",
                                        "Approve this action?",
                                    ]
                                )
                                components = deps.ask_confirmation_components(request_id)
                            else:
                                content = f"<@{user_id}> Command failed: ask confirmation payload was incomplete."
                        else:
                            content = deps.build_command_followup_message(
                                session=session,
                                tenant=tenant,
                                user_id=user_id,
                                command_response=command_response,
                            )
                            raw_issue_key = str(data.get("issue_key") or "").strip().upper()
                            if ISSUE_KEY_PATTERN.fullmatch(raw_issue_key):
                                issue_key = raw_issue_key
                            if command_response.command == "reply" and bool(data.get("recheck_required")):
                                components = deps.ask_reply_components()
                            if command_response.command == "ask" and not reply_to_message_id:
                                initial_thread_attempted = True
                                try:
                                    deps.transport_executor.execute(
                                        action=DiscordAskWithThreadAction(
                                            tenant_id=tenant.tenant_id,
                                            channel_id=channel_id,
                                            user_id=user_id,
                                            content=content,
                                            components=components,
                                            issue_key=issue_key,
                                            followup_context_type="ask_thread",
                                        )
                                    )
                                    sent_to_thread = True
                                except (DiscordApiError, RuntimeError, ValueError) as exc:
                                    logger.exception(
                                        "discord_ask_thread_send_failed tenant_id=%s user_id=%s error=%s",
                                        tenant_id,
                                        user_id,
                                        exc,
                                    )
                                    components = deps.ask_reply_components()
                            elif command_response.command == "issues" and bool(data.get("requires_input")) and not reply_to_message_id:
                                initial_thread_attempted = True
                                followup_request_id = str(data.get("followup_request_id") or "").strip()
                                question_values = data.get("questions")
                                questions = (
                                    [str(value).strip() for value in question_values if str(value).strip()]
                                    if isinstance(question_values, list)
                                    else []
                                )
                                if followup_request_id:
                                    try:
                                        deps.transport_executor.execute(
                                            action=DiscordSeedWithThreadAction(
                                                tenant_id=tenant.tenant_id,
                                                channel_id=channel_id,
                                                user_id=user_id,
                                                content=content,
                                                request_id=followup_request_id,
                                                questions=questions,
                                            )
                                        )
                                        sent_to_thread = True
                                    except (DiscordApiError, RuntimeError, ValueError) as exc:
                                        logger.exception(
                                            "discord_seed_followup_thread_send_failed tenant_id=%s user_id=%s error=%s",
                                            tenant_id,
                                            user_id,
                                            exc,
                                        )
                    except HTTPException as exc:
                        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
                        logger.exception(
                            "discord_command_followup_http_error tenant_id=%s user_id=%s channel_id=%s detail=%s error=%s",
                            tenant_id,
                            user_id,
                            channel_id,
                            detail,
                            exc,
                        )
                        content = f"<@{user_id}> Command failed: {detail}"
                    except Exception as exc:  # pragma: no cover
                        error_ref = uuid4().hex[:8]
                        logger.exception(
                            "discord_command_followup_failed tenant_id=%s user_id=%s error_ref=%s error=%s",
                            tenant_id,
                            user_id,
                            error_ref,
                            exc,
                        )
                        emit_hard_error(
                            event="discord_command_followup_failed",
                            error_ref=error_ref,
                            exc=exc,
                            context={"tenant_id": tenant_id, "user_id": user_id, "channel_id": channel_id},
                        )
                        content = f"<@{user_id}> Command failed due to an internal error. Ref: `{error_ref}`"
                    if reply_to_message_id and not sent_to_thread:
                        try:
                            deps.transport_executor.execute(
                                action=DiscordThreadReplyAction(
                                    tenant_id=tenant.tenant_id,
                                    channel_id=channel_id,
                                    reply_to_message_id=reply_to_message_id,
                                    content=content,
                                    components=components,
                                )
                            )
                            sent_to_thread = True
                        except (DiscordApiError, RuntimeError, ValueError) as exc:
                            logger.exception(
                                "discord_thread_followup_send_failed tenant_id=%s user_id=%s message_id=%s error=%s",
                                tenant_id,
                                user_id,
                                reply_to_message_id,
                                exc,
                            )
                    if not sent_to_thread and not reply_to_message_id and not initial_thread_attempted:
                        try:
                            deps.transport_executor.execute(
                                action=DiscordAskWithThreadAction(
                                    tenant_id=tenant.tenant_id,
                                    channel_id=channel_id,
                                    user_id=user_id,
                                    content=content,
                                    components=components,
                                    issue_key=issue_key,
                                    followup_context_type=_followup_context_type_for_message(
                                        command_name=None,
                                        content=content,
                                        issue_key=issue_key,
                                    ),
                                )
                            )
                            sent_to_thread = True
                        except (DiscordApiError, RuntimeError, ValueError) as exc:
                            logger.exception(
                                "discord_command_thread_send_failed tenant_id=%s user_id=%s error=%s",
                                tenant_id,
                                user_id,
                                exc,
                            )
        except Exception as exc:  # pragma: no cover
            error_ref = uuid4().hex[:8]
            logger.exception(
                "discord_command_followup_runtime_failed tenant_id=%s user_id=%s error_ref=%s error=%s",
                tenant_id,
                user_id,
                error_ref,
                exc,
            )
            emit_hard_error(
                event="discord_command_followup_runtime_failed",
                error_ref=error_ref,
                exc=exc,
                context={"tenant_id": tenant_id, "user_id": user_id, "channel_id": channel_id},
            )
            content = f"<@{user_id}> Command failed due to an internal error. Ref: `{error_ref}`"
        if not sent_to_thread:
            try:
                deps.transport_executor.execute(
                    action=DiscordInteractionFollowupAction(
                        application_id=application_id,
                        interaction_token=interaction_token,
                        content=content,
                        ephemeral=False,
                        components=components,
                        reply_to_message_id=reply_to_message_id,
                        channel_id=channel_id,
                    )
                )
            except DiscordInteractionWebhookExpiredError as exc:
                logger.exception(
                    "discord_command_followup_interaction_expired tenant_id=%s user_id=%s channel_id=%s error=%s",
                    tenant_id,
                    user_id,
                    channel_id,
                    exc,
                )
                if settings is not None:
                    try:
                        with deps.session_factory() as session:
                            tenant = session.get(Tenant, tenant_id)
                            if tenant is not None and tenant.is_enabled:
                                fallback_message_id = reply_to_message_id or f"interaction-{correlation_id}"
                                deps.transport_executor.execute(
                                    action=DiscordThreadReplyAction(
                                        tenant_id=tenant.tenant_id,
                                        channel_id=channel_id,
                                        reply_to_message_id=fallback_message_id,
                                        content=content,
                                        components=components,
                                    )
                                )
                                sent_to_thread = True
                    except Exception as fallback_exc:  # pragma: no cover
                        logger.exception(
                            "discord_command_followup_interaction_expired_fallback_failed tenant_id=%s user_id=%s channel_id=%s error=%s",
                            tenant_id,
                            user_id,
                            channel_id,
                            fallback_exc,
                        )
                if sent_to_thread:
                    return
                _emit_followup_send_failed(
                    event="discord_command_followup_send_failed",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    channel_id=channel_id,
                    exc=exc,
                )
            except Exception as exc:  # pragma: no cover
                _emit_followup_send_failed(
                    event="discord_command_followup_send_failed",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    channel_id=channel_id,
                    exc=exc,
                )
    finally:
        if context_tokens is not None:
            reset_log_context(context_tokens)


async def run_discord_ask_confirmation_followup(
    *,
    deps: DiscordFollowupExecutionDeps,
    tenant_id: str,
    user_id: str,
    channel_id: str,
    decision: str,
    request_id: str,
    application_id: str,
    interaction_token: str,
) -> None:
    content = f"<@{user_id}> Failed to process ask confirmation."
    try:
        with deps.session_factory() as session:
            tenant = session.get(Tenant, tenant_id)
            if tenant is None or not tenant.is_enabled:
                content = f"<@{user_id}> Ask confirmation failed: tenant is unavailable."
            else:
                pending = deps.consume_pending_ask_action(
                    session=session,
                    tenant=tenant,
                    request_id=request_id,
                )
                if pending is None:
                    content = f"<@{user_id}> This ask approval request is no longer available."
                else:
                    pending_user_id = str(pending.get("user_id") or "").strip()
                    pending_channel_id = str(pending.get("channel_id") or "").strip()
                    if pending_user_id and pending_user_id != user_id:
                        content = f"<@{user_id}> Only the original requester can approve or reject this action."
                    elif pending_channel_id and pending_channel_id != channel_id:
                        content = f"<@{user_id}> This ask approval is tied to a different channel."
                    elif decision == "reject":
                        content = f"<@{user_id}> Action rejected. No changes were made."
                    else:
                        proposed_command = str(pending.get("proposed_command") or "").strip()
                        if not proposed_command:
                            content = f"<@{user_id}> Ask approval failed: missing proposed command."
                        elif command_matches(proposed_command, command_name="ask"):
                            content = f"<@{user_id}> Ask approval failed: recursive ask actions are not allowed."
                        else:
                            try:
                                command_response = deps.execute_command_ingress(
                                    tenant_id=tenant_id,
                                    payload=deps.command_request_factory(
                                        user_id=user_id,
                                        command=proposed_command,
                                        channel_id=channel_id,
                                    ),
                                    session=session,
                                    defer_seed_issues=False,
                                    require_ask_confirmation=False,
                                    ingress_source="discord",
                                )
                                content = deps.build_command_followup_message(
                                    session=session,
                                    tenant=tenant,
                                    user_id=user_id,
                                    command_response=command_response,
                                )
                            except HTTPException as exc:
                                detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
                                logger.exception(
                                    "discord_ask_approval_http_error tenant_id=%s user_id=%s channel_id=%s detail=%s error=%s",
                                    tenant_id,
                                    user_id,
                                    channel_id,
                                    detail,
                                    exc,
                                )
                                content = f"<@{user_id}> Approved action failed: {detail}"
                            except Exception as exc:  # pragma: no cover
                                error_ref = uuid4().hex[:8]
                                logger.exception(
                                    "discord_ask_approval_execute_failed tenant_id=%s user_id=%s error_ref=%s error=%s",
                                    tenant_id,
                                    user_id,
                                    error_ref,
                                    exc,
                                )
                                emit_hard_error(
                                    event="discord_ask_approval_execute_failed",
                                    error_ref=error_ref,
                                    exc=exc,
                                    context={"tenant_id": tenant_id, "user_id": user_id, "channel_id": channel_id},
                                )
                                content = f"<@{user_id}> Approved action failed due to an internal error. Ref: `{error_ref}`"
    except Exception as exc:  # pragma: no cover
        error_ref = uuid4().hex[:8]
        logger.exception(
            "discord_ask_approval_runtime_failed tenant_id=%s user_id=%s error_ref=%s error=%s",
            tenant_id,
            user_id,
            error_ref,
            exc,
        )
        emit_hard_error(
            event="discord_ask_approval_runtime_failed",
            error_ref=error_ref,
            exc=exc,
            context={"tenant_id": tenant_id, "user_id": user_id, "channel_id": channel_id},
        )
        content = f"<@{user_id}> Approved action failed due to an internal error. Ref: `{error_ref}`"
    try:
        deps.transport_executor.execute(
            action=DiscordInteractionFollowupAction(
                application_id=application_id,
                interaction_token=interaction_token,
                content=content,
                ephemeral=False,
            )
        )
    except Exception as exc:  # pragma: no cover
        _emit_followup_send_failed(
            event="discord_ask_approval_send_failed",
            tenant_id=tenant_id,
            user_id=user_id,
            channel_id=channel_id,
            exc=exc,
        )


def _emit_followup_send_failed(*, event: str, tenant_id: str, user_id: str, channel_id: str, exc: Exception) -> None:
    error_ref = uuid4().hex[:8]
    logger.exception(
        "%s tenant_id=%s user_id=%s error_ref=%s error=%s",
        event,
        tenant_id,
        user_id,
        error_ref,
        exc,
    )
    emit_hard_error(
        event=event,
        error_ref=error_ref,
        exc=exc,
        context={"tenant_id": tenant_id, "user_id": user_id, "channel_id": channel_id},
    )
