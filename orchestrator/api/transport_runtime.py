from __future__ import annotations

import asyncio
import logging
import json
from typing import Any
from collections.abc import Callable, Iterable
from contextlib import nullcontext

from fastapi.responses import JSONResponse, Response

from orchestrator.core.communications import (
    HttpJsonResponseAction,
    HttpJsonResponseBytesAction,
    IngressResult,
    TransportEnvelope,
    TransportAction,
)
from orchestrator.core.communications.integration_contracts import (
    TransportActionExecutor,
)
from orchestrator.core.discord.transport_executor import DiscordTransportExecutor
from orchestrator.core.observability.otel import current_log_context
from orchestrator.core.platform.secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    resolve_platform_secret_ref,
)

logger = logging.getLogger(__name__)


class HttpTransportExecutor:
    def execute(self, *, action: TransportAction) -> Response:
        if isinstance(action, HttpJsonResponseAction):
            return JSONResponse(
                status_code=action.status_code,
                content=dict(action.content),
                headers=dict(action.headers),
            )
        if isinstance(action, HttpJsonResponseBytesAction):
            body = (
                action.body.encode("utf-8")
                if isinstance(action.body, str)
                else action.body
            )
            return Response(
                status_code=action.status_code,
                content=body,
                media_type="application/json",
                headers=dict(action.headers),
            )
        raise RuntimeError(
            f"Unsupported HTTP transport action: {type(action).__name__}"
        )


def http_json_response_action(
    *, status_code: int, content: dict
) -> HttpJsonResponseAction:
    return HttpJsonResponseAction(status_code=status_code, content=content)


def json_response_to_action(response: JSONResponse) -> HttpJsonResponseBytesAction:
    return HttpJsonResponseBytesAction(
        status_code=response.status_code,
        body=response.body,
        headers=dict(response.headers),
    )


def execute_http_ingress_result(
    *,
    result: IngressResult,
    task_scheduler=asyncio.create_task,
    envelope: TransportEnvelope | None = None,
    transport_action_executors: Iterable[TransportActionExecutor] = (),
) -> Response:
    executor_tuple = tuple(transport_action_executors)
    _log_ingress_result_built(result=result, envelope=envelope)
    _schedule_deferred_work(
        deferred_work=result.deferred_work,
        envelope=envelope,
        task_scheduler=task_scheduler,
    )
    response_action: TransportAction | None = None
    for action in result.actions:
        if isinstance(action, (HttpJsonResponseAction, HttpJsonResponseBytesAction)):
            if response_action is None:
                response_action = action
            continue
        execute_side_effect_action(
            action=action,
            envelope=envelope,
            transport_action_executors=executor_tuple,
        )
    if response_action is None:
        raise RuntimeError(
            "IngressResult for HTTP transport did not include an HTTP response action"
        )
    _log_transport_runtime_event(
        "transport_http_response_selected",
        envelope=envelope,
        response_action_type=type(response_action).__name__,
        status_code=getattr(response_action, "status_code", None),
    )
    return HttpTransportExecutor().execute(action=response_action)


def execute_side_effect_ingress_result(
    *,
    result: IngressResult,
    task_scheduler=asyncio.create_task,
    envelope: TransportEnvelope | None = None,
    transport_action_executors: Iterable[TransportActionExecutor] = (),
    action_error_handler: Callable[[TransportAction, Exception], bool] | None = None,
) -> None:
    executor_tuple = tuple(transport_action_executors)
    _log_ingress_result_built(result=result, envelope=envelope)
    _schedule_deferred_work(
        deferred_work=result.deferred_work,
        envelope=envelope,
        task_scheduler=task_scheduler,
    )
    for action in result.actions:
        try:
            execute_side_effect_action(
                action=action,
                envelope=envelope,
                transport_action_executors=executor_tuple,
            )
        except Exception as exc:  # noqa: BLE001
            if action_error_handler is not None and action_error_handler(action, exc):
                continue
            raise


def execute_side_effect_action(
    *,
    action: TransportAction,
    envelope: TransportEnvelope | None,
    transport_action_executors: tuple[TransportActionExecutor, ...],
) -> dict[str, Any] | None:
    last_unsupported_error: Exception | None = None
    for executor in transport_action_executors:
        try:
            result = executor.execute(action=action)
            _log_transport_runtime_event(
                "transport_action_executed",
                envelope=envelope,
                action_type=type(action).__name__,
                executor_type=type(executor).__name__,
                outcome="success",
            )
            return result if isinstance(result, dict) else None
        except RuntimeError as exc:
            if "Unsupported" in str(exc):
                last_unsupported_error = exc
                continue
            _log_transport_runtime_event(
                "transport_action_failed",
                envelope=envelope,
                action_type=type(action).__name__,
                executor_type=type(executor).__name__,
                outcome="error",
                error=str(exc),
                level="exception",
            )
            raise
        except Exception as exc:  # noqa: BLE001
            _log_transport_runtime_event(
                "transport_action_failed",
                envelope=envelope,
                action_type=type(action).__name__,
                executor_type=type(executor).__name__,
                outcome="error",
                error=str(exc),
                level="exception",
            )
            raise
    if last_unsupported_error is not None:
        raise RuntimeError(
            f"No HTTP transport executor handled action {type(action).__name__}"
        ) from last_unsupported_error
    raise RuntimeError(
        f"HTTP ingress result included unsupported non-HTTP action: {type(action).__name__}"
    )


def build_discord_transport_executor(
    *,
    bot_token: str | None = None,
    interaction_callback_sender=None,
    interaction_followup_sender=None,
    client_factory=None,
    session_factory: Callable | None = None,  # noqa: ANN401
    settings_factory: Callable | None = None,  # noqa: ANN401
    resolve_platform_secret_ref_fn=None,  # noqa: ANN401
    thread_followup_sender=None,
    ask_with_thread_sender=None,
    seed_with_thread_sender=None,
) -> DiscordTransportExecutor:
    kwargs = {}
    if bot_token is not None:
        kwargs["bot_token"] = bot_token
    if interaction_callback_sender is not None:
        kwargs["interaction_callback_sender"] = interaction_callback_sender
    if interaction_followup_sender is not None:
        kwargs["interaction_followup_sender"] = interaction_followup_sender
    if client_factory is not None:
        kwargs["client_factory"] = client_factory
    if session_factory is not None:
        kwargs["session_factory"] = session_factory
    if settings_factory is not None:
        kwargs["settings_factory"] = settings_factory
    if resolve_platform_secret_ref_fn is not None:
        kwargs["resolve_platform_secret_ref_fn"] = resolve_platform_secret_ref_fn
    if thread_followup_sender is not None:
        kwargs["thread_followup_sender"] = thread_followup_sender
    if ask_with_thread_sender is not None:
        kwargs["ask_with_thread_sender"] = ask_with_thread_sender
    if seed_with_thread_sender is not None:
        kwargs["seed_with_thread_sender"] = seed_with_thread_sender
    return DiscordTransportExecutor(**kwargs)


def build_transport_action_executors(
    *,
    extra_transport_action_executors: Iterable[TransportActionExecutor] = (),
    discord_transport_executor: TransportActionExecutor | None = None,
) -> tuple[TransportActionExecutor, ...]:
    executors = tuple(extra_transport_action_executors)
    if discord_transport_executor is None:
        return executors
    return (*executors, discord_transport_executor)


def build_http_transport_action_executors(
    *,
    session,
    settings,  # noqa: ANN001
    extra_transport_action_executors: Iterable[TransportActionExecutor] = (),
    resolve_platform_secret_ref_fn=None,  # noqa: ANN401
) -> tuple[TransportActionExecutor, ...]:
    token_resolver = resolve_platform_secret_ref_fn or resolve_platform_secret_ref
    token_ref = str(PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF or "").strip()
    bot_token = (
        token_resolver(
            session,
            secret_ref=token_ref,
            encryption_key=settings.secrets_encryption_key,
        )
        if token_ref
        else None
    )
    return build_transport_action_executors(
        extra_transport_action_executors=extra_transport_action_executors,
        discord_transport_executor=build_discord_transport_executor(
            bot_token=bot_token,
            session_factory=lambda: nullcontext(session),
            settings_factory=lambda: settings,
        ),
    )


def _log_transport_runtime_event(
    event: str,
    *,
    envelope: TransportEnvelope | None,
    level: str = "info",
    **metadata,
) -> None:
    context = current_log_context()
    tenant_id = (
        (envelope.tenant_id_hint if envelope is not None else None)
        or context.get("tenant_id")
        or ""
    )
    log = getattr(logger, level)
    log(
        "%s transport=%s event_type=%s request_id=%s tenant_id=%s project_id=%s metadata=%s",
        event,
        envelope.transport if envelope is not None else "",
        envelope.event_type if envelope is not None else "",
        envelope.request_id if envelope is not None else "",
        tenant_id,
        context.get("project_id") or "",
        metadata,
    )


def _log_ingress_result_built(
    *, result: IngressResult, envelope: TransportEnvelope | None
) -> None:
    _log_transport_runtime_event(
        "transport_ingress_result_built",
        envelope=envelope,
        action_count=len(result.actions),
        deferred_count=len(result.deferred_work),
    )


def _schedule_deferred_work(
    *,
    deferred_work,
    envelope: TransportEnvelope | None,
    task_scheduler,
) -> None:
    for deferred in deferred_work:
        task = task_scheduler(deferred.runner())
        _log_transport_runtime_event(
            "transport_deferred_scheduled",
            envelope=envelope,
            deferred_kind=deferred.kind,
        )
        if deferred.on_scheduled is not None:
            deferred.on_scheduled(task)


def decode_json_body(action: TransportAction) -> dict:
    if isinstance(action, HttpJsonResponseAction):
        return dict(action.content)
    if not isinstance(action, HttpJsonResponseBytesAction):
        raise TypeError(
            f"Transport action body was not an HTTP JSON action: {type(action).__name__}"
        )
    body = action.body
    if isinstance(body, bytes):
        return json.loads(body.decode("utf-8"))
    if isinstance(body, str):
        return json.loads(body)
    raise TypeError("Transport action body was not JSON bytes or string")
