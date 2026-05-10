from __future__ import annotations

from datetime import datetime, timezone

from orchestrator.api.schemas import PlatformServiceStatusRead, PlatformStatusRead
from orchestrator.core.config import Settings
from orchestrator.core.discord.command_sync_status import get_discord_command_sync_status
from orchestrator.core.knowledge.jira_sync_status import get_runtime_status as get_knowledge_jira_sync_runtime_status
from orchestrator.core.worker.runtime_status_service import build_worker_service_status


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _api_status() -> PlatformServiceStatusRead:
    return PlatformServiceStatusRead(
        service_id="api",
        label="API",
        status="healthy",
        summary="Serving admin and workspace requests.",
        updated_at=_utcnow(),
        capabilities=[],
        runtime_dependencies={},
    )


def _worker_status(*, session, settings: Settings) -> PlatformServiceStatusRead:  # noqa: ANN001
    snapshot = build_worker_service_status(session=session, settings=settings)
    return PlatformServiceStatusRead(
        service_id="workers",
        label="Workers",
        status=snapshot.status,
        summary=snapshot.summary,
        updated_at=snapshot.updated_at,
        capabilities=snapshot.capabilities,
        runtime_dependencies=snapshot.runtime_dependencies,
        instances=[
            {
                "instance_id": instance.instance_id,
                "label": instance.label,
                "status": instance.status,
                "summary": instance.summary,
                "updated_at": instance.updated_at,
                "capabilities": instance.capabilities,
                "active_run_count": instance.active_run_count,
                "runtime_dependencies": instance.runtime_dependencies,
            }
            for instance in snapshot.instances
        ],
    )


def _knowledge_sync_status(*, session, settings: Settings) -> PlatformServiceStatusRead:  # noqa: ANN001
    runtime = get_knowledge_jira_sync_runtime_status(session=session, settings=settings)
    updated_at = (
        _coerce_aware(runtime.last_heartbeat_at)
        or _coerce_aware(runtime.last_pass_finished_at)
        or _coerce_aware(runtime.last_pass_started_at)
        or _coerce_aware(runtime.started_at)
        or _coerce_aware(runtime.stopped_at)
    )
    if runtime.state in {"running"} and not runtime.stale:
        status = "healthy"
        summary = "Project knowledge sync is running normally."
    elif runtime.state in {"stale", "degraded"} or runtime.stale:
        status = "degraded"
        summary = "Project knowledge sync needs attention."
    elif runtime.state in {"disabled", "skipped_non_postgres"}:
        status = "unavailable"
        summary = "Project knowledge sync is disabled for this deployment."
    else:
        status = "idle"
        summary = "Project knowledge sync has not started a pass yet."
    return PlatformServiceStatusRead(
        service_id="knowledge_jira_sync",
        label="Knowledge sync",
        status=status,
        summary=summary,
        updated_at=updated_at,
        capabilities=[],
        runtime_dependencies={},
    )


def _discord_commands_status(*, session) -> PlatformServiceStatusRead:  # noqa: ANN001
    runtime = get_discord_command_sync_status(session=session)
    updated_at = _coerce_aware(runtime.last_success_at) or _coerce_aware(runtime.last_attempt_at)
    if runtime.healthy and runtime.synced:
        status = "healthy"
        summary = f"{runtime.command_count} Discord command{'' if runtime.command_count == 1 else 's'} synced."
    elif not runtime.bot_token_configured or not runtime.guild_id_configured:
        status = "unavailable"
        summary = "Discord commands are not configured for this deployment."
    elif runtime.last_failure_reason or runtime.last_error:
        status = "degraded"
        summary = "Discord command sync reported a recent failure."
    else:
        status = "idle"
        summary = "Discord command sync is waiting for its first successful run."
    return PlatformServiceStatusRead(
        service_id="discord_commands",
        label="Discord commands",
        status=status,
        summary=summary,
        updated_at=updated_at,
        capabilities=[],
        runtime_dependencies={},
    )


def platform_status(*, session, settings: Settings) -> PlatformStatusRead:  # noqa: ANN001
    return PlatformStatusRead(
        services=[
            _api_status(),
            _worker_status(session=session, settings=settings),
            _knowledge_sync_status(session=session, settings=settings),
            _discord_commands_status(session=session),
        ]
    )
