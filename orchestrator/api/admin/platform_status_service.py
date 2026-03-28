from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from orchestrator.api.schemas import PlatformServiceInstanceRead, PlatformServiceStatusRead, PlatformStatusRead
from orchestrator.core.config import Settings
from orchestrator.core.discord.command_sync_status import get_discord_command_sync_status
from orchestrator.core.knowledge_jira_sync_status import get_runtime_status as get_knowledge_jira_sync_runtime_status
from orchestrator.core.worker_capabilities import parse_worker_capabilities
from orchestrator.storage.models import Run, WorkerRuntimeState

ACTIVE_RUN_STATUSES = {"queued", "running"}
WORKER_RUNTIME_HEARTBEAT_STALE_SECONDS = 120


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _capability_label(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "macos":
        return "macOS"
    return normalized.capitalize()


def _worker_row_capabilities(row: WorkerRuntimeState) -> list[str]:
    capabilities = parse_worker_capabilities(row.capabilities_json)
    return [_capability_label(item) for item in sorted(capabilities)]


def _is_worker_row_fresh(*, row: WorkerRuntimeState, now: datetime) -> bool:
    last_seen = _coerce_aware(row.last_heartbeat_at) or _coerce_aware(row.updated_at)
    if last_seen is None:
        return False
    return last_seen >= (now - timedelta(seconds=WORKER_RUNTIME_HEARTBEAT_STALE_SECONDS))


def _worker_instance_status(
    *,
    row: WorkerRuntimeState,
    now: datetime,
    active_run_count: int,
) -> tuple[str, str]:
    state = str(row.state or "").strip().lower()
    if state == "stopped":
        return "stopped", "Worker stopped cleanly."
    if not _is_worker_row_fresh(row=row, now=now):
        return "stale", "Last heartbeat is outside the worker freshness window."
    if active_run_count > 0 or state == "busy":
        run_label = "run" if active_run_count == 1 else "runs"
        return "busy", f"Processing {active_run_count} active {run_label}."
    if state == "starting":
        return "starting", "Worker is starting up."
    return "idle", "Worker is idle and ready for work."


def _worker_instances(*, session, now: datetime) -> list[PlatformServiceInstanceRead]:  # noqa: ANN001
    run_counts = dict(
        session.execute(
            select(
                Run.worker_service_instance_id,
                func.count(Run.run_id),
            )
            .where(
                Run.status.in_(ACTIVE_RUN_STATUSES),
                Run.worker_service_instance_id.is_not(None),
            )
            .group_by(Run.worker_service_instance_id)
        ).all()
    )
    rows = session.execute(select(WorkerRuntimeState).order_by(WorkerRuntimeState.service_instance_id.asc())).scalars().all()
    instances: list[PlatformServiceInstanceRead] = []
    for row in rows:
        active_run_count = int(run_counts.get(row.service_instance_id, 0) or 0)
        instance_status, instance_summary = _worker_instance_status(
            row=row,
            now=now,
            active_run_count=active_run_count,
        )
        last_seen = _coerce_aware(row.last_heartbeat_at) or _coerce_aware(row.updated_at)
        instances.append(
            PlatformServiceInstanceRead(
                instance_id=row.service_instance_id,
                label=row.service_instance_id,
                status=instance_status,
                summary=instance_summary,
                updated_at=last_seen,
                capabilities=_worker_row_capabilities(row),
                active_run_count=active_run_count,
            )
        )
    return instances


def _worker_capabilities(*, instances: list[PlatformServiceInstanceRead], settings: Settings) -> list[str]:
    capability_ids: set[str] = set()
    for instance in instances:
        capability_ids.update(
            {
                capability.lower().strip()
                for capability in instance.capabilities
                if str(capability).strip()
            }
        )
    if not capability_ids:
        capability_ids.update(parse_worker_capabilities(getattr(settings, "worker_capabilities", None)))
    return [_capability_label(item) for item in sorted(capability_ids)]


def _api_status() -> PlatformServiceStatusRead:
    return PlatformServiceStatusRead(
        service_id="api",
        label="API",
        status="healthy",
        summary="Serving admin and workspace requests.",
        updated_at=_utcnow(),
        capabilities=[],
    )


def _worker_status(*, session, settings: Settings) -> PlatformServiceStatusRead:  # noqa: ANN001
    now = _utcnow()
    instances = _worker_instances(session=session, now=now)
    capabilities = _worker_capabilities(instances=instances, settings=settings)
    fresh_instances = [instance for instance in instances if instance.status not in {"stale", "stopped"}]
    stale_instances = [instance for instance in instances if instance.status == "stale"]
    busy_instances = [instance for instance in fresh_instances if instance.status == "busy"]
    latest_heartbeat = max((instance.updated_at for instance in instances if instance.updated_at is not None), default=None)
    if fresh_instances:
        if stale_instances:
            status = "degraded"
            summary = f"{len(stale_instances)} worker instance{'' if len(stale_instances) == 1 else 's'} have stale heartbeats."
        elif busy_instances:
            busy_count = sum(instance.active_run_count for instance in busy_instances)
            summary = (
                f"{len(fresh_instances)} worker instance{'' if len(fresh_instances) == 1 else 's'} online, "
                f"{busy_count} active run{'' if busy_count == 1 else 's'} in progress."
            )
            status = "healthy"
        else:
            status = "idle"
            summary = f"{len(fresh_instances)} worker instance{'' if len(fresh_instances) == 1 else 's'} online and idle."
    elif stale_instances:
        status = "degraded"
        summary = f"{len(stale_instances)} worker instance{'' if len(stale_instances) == 1 else 's'} have stale heartbeats."
    elif instances:
        status = "unavailable"
        summary = "Worker instances are stopped."
    else:
        status = "unavailable"
        summary = "No worker runtime registrations are present."

    return PlatformServiceStatusRead(
        service_id="workers",
        label="Workers",
        status=status,
        summary=summary,
        updated_at=latest_heartbeat,
        capabilities=capabilities,
        instances=instances,
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
