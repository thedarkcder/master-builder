from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from orchestrator.core.communications.integration_contracts import InteractiveReplyTransport
from orchestrator.core.error_observability import emit_hard_error
from orchestrator.core.observability import reset_log_context, set_log_context
from orchestrator.storage.models import Tenant
from orchestrator.tools.discord_api import DiscordApiError

logger = logging.getLogger(__name__)


class DiscordWebhookFollowupService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], AbstractContextManager[Session]],
        settings_factory: Callable[[], object],
        execute_command_ingress: Callable[..., object],
        command_request_factory: Callable[..., object],
        build_command_followup_message: Callable[..., str],
        ask_confirmation_components: Callable[[str], list[dict]],
        ask_reply_components: Callable[[], list[dict]],
        reply_transport: InteractiveReplyTransport,
        consume_pending_ask_action: Callable[..., dict | None],
    ) -> None:
        self._session_factory = session_factory
        self._settings_factory = settings_factory
        self._execute_command_ingress = execute_command_ingress
        self._command_request_factory = command_request_factory
        self._build_command_followup_message = build_command_followup_message
        self._ask_confirmation_components = ask_confirmation_components
        self._ask_reply_components = ask_reply_components
        self._reply_transport = reply_transport
        self._consume_pending_ask_action = consume_pending_ask_action

    async def run_discord_command_followup(
        self,
        *,
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
        content = f"<@{user_id}> Command failed due to an internal error."
        components: list[dict] | None = None
        sent_to_thread = False
        try:
            context_tokens = set_log_context(
                correlation_id=correlation_id,
                tenant_id=tenant_id,
                agent_id=user_id,
            )
            settings = self._settings_factory()
            try:
                with self._session_factory() as session:
                    tenant = session.get(Tenant, tenant_id)
                    if tenant is None or not tenant.is_enabled:
                        content = f"<@{user_id}> Command failed: tenant is unavailable."
                    else:
                        try:
                            command_response = self._execute_command_ingress(
                                tenant_id=tenant_id,
                                payload=self._command_request_factory(
                                    user_id=user_id,
                                    command=command_text,
                                    channel_id=channel_id,
                                    command_params=command_params,
                                    attachments=attachments or [],
                                ),
                                session=session,
                                defer_seed_issues=False,
                                require_ask_confirmation=True,
                                ingress_source="discord",
                            )
                            data = command_response.data if isinstance(command_response.data, dict) else {}
                            requires_confirmation = (
                                bool(data.get("requires_confirmation")) and command_response.command == "ask"
                            )
                            if requires_confirmation:
                                request_id = str(data.get("request_id") or "").strip()
                                proposed_command = str(data.get("proposed_command") or "").strip()
                                summary = str(data.get("summary") or command_response.message or "").strip()
                                if request_id and proposed_command:
                                    lines = [
                                        f"<@{user_id}> {summary}",
                                        "",
                                        f"Proposed action: `{proposed_command}`",
                                        "Approve this action?",
                                    ]
                                    content = "\n".join(lines)
                                    components = self._ask_confirmation_components(request_id)
                                else:
                                    content = f"<@{user_id}> Command failed: ask confirmation payload was incomplete."
                            else:
                                content = self._build_command_followup_message(
                                    session=session,
                                    tenant=tenant,
                                    user_id=user_id,
                                    command_response=command_response,
                                )
                                if command_response.command == "ask" and not reply_to_message_id:
                                    try:
                                        self._reply_transport.send_ask_with_thread(
                                            session=session,
                                            settings=settings,
                                            tenant=tenant,
                                            channel_id=channel_id,
                                            user_id=user_id,
                                            content=content,
                                        )
                                        sent_to_thread = True
                                    except (DiscordApiError, RuntimeError, ValueError) as exc:
                                        logger.exception(
                                            "discord_ask_thread_send_failed tenant_id=%s user_id=%s error=%s",
                                            tenant_id,
                                            user_id,
                                            exc,
                                        )
                                        components = self._ask_reply_components()
                                elif (
                                    command_response.command == "issues"
                                    and bool(data.get("requires_input"))
                                    and not reply_to_message_id
                                ):
                                    followup_request_id = str(data.get("followup_request_id") or "").strip()
                                    question_values = data.get("questions")
                                    questions = (
                                        [str(value).strip() for value in question_values if str(value).strip()]
                                        if isinstance(question_values, list)
                                        else []
                                    )
                                    if followup_request_id:
                                        try:
                                            self._reply_transport.send_seed_with_thread(
                                                session=session,
                                                settings=settings,
                                                tenant=tenant,
                                                channel_id=channel_id,
                                                user_id=user_id,
                                                content=content,
                                                request_id=followup_request_id,
                                                questions=questions,
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
                        except Exception as exc:  # pragma: no cover - defensive logging path
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
                                context={
                                    "tenant_id": tenant_id,
                                    "user_id": user_id,
                                    "channel_id": channel_id,
                                },
                            )
                            content = f"<@{user_id}> Command failed due to an internal error. Ref: `{error_ref}`"
                        if reply_to_message_id and not sent_to_thread:
                            try:
                                self._reply_transport.send_thread_reply(
                                    session=session,
                                    settings=settings,
                                    tenant=tenant,
                                    channel_id=channel_id,
                                    reply_to_message_id=reply_to_message_id,
                                    content=content,
                                    components=components,
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
            except Exception as exc:  # pragma: no cover - defensive logging path
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
                    context={
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "channel_id": channel_id,
                    },
                )
                content = f"<@{user_id}> Command failed due to an internal error. Ref: `{error_ref}`"
            if not sent_to_thread:
                try:
                    self._reply_transport.send_interaction_followup(
                        application_id=application_id,
                        interaction_token=interaction_token,
                        content=content,
                        ephemeral=False,
                        components=components,
                        reply_to_message_id=reply_to_message_id,
                        channel_id=channel_id,
                    )
                except Exception as exc:  # pragma: no cover - defensive logging path
                    error_ref = uuid4().hex[:8]
                    logger.exception(
                        "discord_command_followup_send_failed tenant_id=%s user_id=%s error_ref=%s error=%s",
                        tenant_id,
                        user_id,
                        error_ref,
                        exc,
                    )
                    emit_hard_error(
                        event="discord_command_followup_send_failed",
                        error_ref=error_ref,
                        exc=exc,
                        context={
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "channel_id": channel_id,
                        },
                    )
        finally:
            if context_tokens is not None:
                reset_log_context(context_tokens)

    async def run_discord_ask_confirmation_followup(
        self,
        *,
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
            with self._session_factory() as session:
                tenant = session.get(Tenant, tenant_id)
                if tenant is None or not tenant.is_enabled:
                    content = f"<@{user_id}> Ask confirmation failed: tenant is unavailable."
                else:
                    pending = self._consume_pending_ask_action(
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
                            elif proposed_command.lower().startswith("!ask"):
                                content = f"<@{user_id}> Ask approval failed: recursive ask actions are not allowed."
                            else:
                                try:
                                    command_response = self._execute_command_ingress(
                                        tenant_id=tenant_id,
                                        payload=self._command_request_factory(
                                            user_id=user_id,
                                            command=proposed_command,
                                            channel_id=channel_id,
                                        ),
                                        session=session,
                                        defer_seed_issues=False,
                                        require_ask_confirmation=False,
                                        ingress_source="discord",
                                    )
                                    content = self._build_command_followup_message(
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
                                except Exception as exc:  # pragma: no cover - defensive path
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
                                        context={
                                            "tenant_id": tenant_id,
                                            "user_id": user_id,
                                            "channel_id": channel_id,
                                        },
                                    )
                                    content = f"<@{user_id}> Approved action failed due to an internal error. Ref: `{error_ref}`"
        except Exception as exc:  # pragma: no cover - defensive logging path
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
                context={
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "channel_id": channel_id,
                },
            )
            content = f"<@{user_id}> Approved action failed due to an internal error. Ref: `{error_ref}`"
        try:
            self._reply_transport.send_interaction_followup(
                application_id=application_id,
                interaction_token=interaction_token,
                content=content,
                ephemeral=False,
            )
        except Exception as exc:  # pragma: no cover - defensive logging path
            error_ref = uuid4().hex[:8]
            logger.exception(
                "discord_ask_approval_send_failed tenant_id=%s user_id=%s error_ref=%s error=%s",
                tenant_id,
                user_id,
                error_ref,
                exc,
            )
            emit_hard_error(
                event="discord_ask_approval_send_failed",
                error_ref=error_ref,
                exc=exc,
                context={
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "channel_id": channel_id,
                },
            )
