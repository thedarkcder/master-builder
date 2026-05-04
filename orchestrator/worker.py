from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.api.admin.tenant_crud import purge_expired_archived_tenants
from orchestrator.core.config import Settings, get_settings
from orchestrator.core.observability.logging import configure_logging
from orchestrator.core.knowledge.prewarm import prewarm_knowledge_dependencies
from orchestrator.core.observability.metrics import platform_metrics
from orchestrator.core.observability.otel_telemetry import initialize_telemetry
from orchestrator.core.projects.policy import resolve_effective_policy
from orchestrator.core.runs.service import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_DISPATCHING,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_WAITING_FOR_INPUT,
)
from orchestrator.core.worker.child_process import WORKER_CHILD_EXIT_DEPENDENCY_FAILURE
from orchestrator.core.worker.child_process import WORKER_CHILD_EXIT_IDLE
from orchestrator.core.worker.child_process import WORKER_CHILD_EXIT_PROCESSED
from orchestrator.core.worker.child_process import WORKER_CHILD_EXIT_RUNTIME_FAILURE
from orchestrator.core.worker.child_process import WorkerChildProcessHandle
from orchestrator.core.worker.child_process import WorkerChildProcessResult
from orchestrator.core.worker.child_process import spawn_worker_child_process as _spawn_worker_child_process
from orchestrator.core.worker.child_process import terminate_worker_child_processes as _terminate_worker_child_processes
from orchestrator.core.worker.run_health import (
    recover_stale_running_runs,
    worker_service_instance_id_for_mode,
)
from orchestrator.core.worker.execution_service import (
    process_next_webhook_job_with_dependencies as _process_next_webhook_job_with_dependencies,
)
from orchestrator.core.worker.queue_listener import (
    RunQueueNotificationBridge,
    wait_for_wake_or_stop,
)
from orchestrator.core.worker.runtime_dependencies import (
    WorkerRuntimeDependencySnapshot,
    registered_worker_runtime_kinds_from_settings,
    sync_worker_runtime_auth_requests,
    stop_all_live_runtime_auth_sessions,
    worker_runtime_dependency_snapshot,
)
from orchestrator.core.worker.run_dispatch import WorkerDependencyFailure
from orchestrator.core.worker.run_dispatch import claim_next_run_once as _claim_next_run_once
from orchestrator.core.worker.run_dispatch import has_available_webhook_job_once as _has_available_webhook_job_once
from orchestrator.core.worker.run_dispatch import probe_claimable_run_once as _probe_claimable_run_once
from orchestrator.core.worker.run_dispatch import process_next_run_once as _process_next_run_once
from orchestrator.core.worker.run_dispatch import reconcile_claimed_run_after_child_exit as _reconcile_claimed_run_after_child_exit
from orchestrator.core.worker.capabilities import resolve_worker_capability_context
from orchestrator.core.workflow.execution_snapshot_startup import ensure_execution_snapshot_startup_bootstrap
from orchestrator.core.workflow.type_catalog import validate_persisted_workflow_definitions
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.run_queue_events import (
    RUN_QUEUE_NOTIFY_CHANNEL,
    is_postgres_database_url,
    postgres_dsn_from_database_url,
)
from orchestrator.storage.models import Project, Run, Tenant, WorkerRuntimeState

try:
    import psycopg
except ImportError:  # pragma: no cover - dependency is required at runtime
    psycopg = None

logger = logging.getLogger(__name__)
WORKER_RUNTIME_HEARTBEAT_INTERVAL_SECONDS = 30
WORKER_MODE_RUNS = "runs"
WORKER_MODE_WEBHOOKS = "webhooks"
WORKER_STARTUP_DB_RETRY_MAX_ATTEMPTS = 6
WORKER_STARTUP_DB_RETRY_BASE_DELAY_SECONDS = 1.0
WORKER_STARTUP_DB_RETRY_MAX_DELAY_SECONDS = 8.0
_VALID_POST_CHILD_RUN_STATUSES = {
    "queued",
    "ownership_lost",
    "temporal_handoff",
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_FOR_INPUT,
    RUN_STATUS_BLOCKED,
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_FAILED,
    RUN_STATUS_CANCELLED,
}
def _coerce_parallel_slots(raw_value: object) -> int:
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return 1
    return max(1, parsed)


def _coerce_non_negative_int(raw_value: object) -> int:
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _worker_runtime_capabilities(*, settings: Settings) -> list[str]:
    context = resolve_worker_capability_context(
        raw_value=getattr(settings, "worker_capabilities", None),
        source="ORCHESTRATOR_WORKER_CAPABILITIES",
    )
    return list(context.available_values)


def _worker_registered_runtime_kinds(*, settings: Settings) -> list[str]:
    return registered_worker_runtime_kinds_from_settings(settings)


def _worker_runtime_active_run_count(
    *,
    session: Session,
    service_instance_id: str,
) -> int:
    count = session.execute(
        select(func.count(Run.run_id)).where(
            Run.status.in_((RUN_STATUS_DISPATCHING, RUN_STATUS_RUNNING)),
            Run.worker_service_instance_id == service_instance_id,
        )
    ).scalar_one()
    return _coerce_non_negative_int(count)


def _upsert_worker_runtime_state_once(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    agent_id: str,
    service_instance_id: str,
    worker_mode: str,
    state: str,
    runtime_dependencies_json: dict[str, dict[str, object]] | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    capabilities = _worker_runtime_capabilities(settings=settings)
    runtime_kinds = _worker_registered_runtime_kinds(settings=settings)
    with session_factory() as session:
        row = session.get(WorkerRuntimeState, service_instance_id)
        if row is None:
            row = WorkerRuntimeState(
                service_instance_id=service_instance_id,
                agent_id=agent_id,
                worker_mode=worker_mode,
                capabilities_json=capabilities,
                runtime_kinds_json=runtime_kinds,
                runtime_dependencies_json=dict(runtime_dependencies_json or {}),
                state=state,
                started_at=now,
                last_heartbeat_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.agent_id = agent_id
            row.worker_mode = worker_mode
            row.capabilities_json = capabilities
            row.runtime_kinds_json = runtime_kinds
            if runtime_dependencies_json is not None:
                row.runtime_dependencies_json = dict(runtime_dependencies_json)
            row.state = state
            row.last_heartbeat_at = now
            row.updated_at = now
            if row.started_at is None:
                row.started_at = now
        session.commit()


def _register_worker_runtime_once(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    agent_id: str,
    service_instance_id: str,
    worker_mode: str,
) -> None:
    _upsert_worker_runtime_state_once(
        session_factory=session_factory,
        settings=settings,
        agent_id=agent_id,
        service_instance_id=service_instance_id,
        worker_mode=worker_mode,
        state="starting",
    )


def _refresh_worker_runtime_once(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    agent_id: str,
    service_instance_id: str,
    worker_mode: str,
) -> None:
    with session_factory() as session:
        active_run_count = _worker_runtime_active_run_count(session=session, service_instance_id=service_instance_id)
        row = session.get(WorkerRuntimeState, service_instance_id)
        existing_state = str(getattr(row, "state", "") or "").strip().lower() if row is not None else ""
        runtime_dependencies_json = dict(getattr(row, "runtime_dependencies_json", {}) or {}) if row is not None else {}
    if active_run_count > 0:
        state = "busy"
    elif existing_state == "degraded":
        state = "degraded"
    else:
        state = "idle"
    _upsert_worker_runtime_state_once(
        session_factory=session_factory,
        settings=settings,
        agent_id=agent_id,
        service_instance_id=service_instance_id,
        worker_mode=worker_mode,
        state=state,
        runtime_dependencies_json=runtime_dependencies_json,
    )


def _sync_run_worker_runtime_dependencies_once(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    agent_id: str,
    service_instance_id: str,
) -> WorkerRuntimeDependencySnapshot:
    snapshot = worker_runtime_dependency_snapshot(
        session_factory=session_factory,
        settings=settings,
        service_instance_id=service_instance_id,
    )
    _upsert_worker_runtime_state_once(
        session_factory=session_factory,
        settings=settings,
        agent_id=agent_id,
        service_instance_id=service_instance_id,
        worker_mode=WORKER_MODE_RUNS,
        state="degraded" if snapshot.degraded else "idle",
        runtime_dependencies_json=snapshot.to_json(),
    )
    return snapshot


def _stop_worker_runtime_once(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    agent_id: str,
    service_instance_id: str,
    worker_mode: str,
) -> None:
    _upsert_worker_runtime_state_once(
        session_factory=session_factory,
        settings=settings,
        agent_id=agent_id,
        service_instance_id=service_instance_id,
        worker_mode=worker_mode,
        state="stopped",
    )


def _resolve_parallel_slots_from_policy(*, session_factory: sessionmaker[Session]) -> int:
    try:
        with session_factory() as session:
            tenants = session.execute(
                select(Tenant).where(Tenant.is_enabled.is_(True))
            ).scalars().all()
            if not tenants:
                return 1
            projects = session.execute(
                select(Project).where(Project.is_archived.is_(False))
            ).scalars().all()
    except Exception as exc:
        logger.exception("worker_parallel_slot_resolution_failed error=%s", exc)
        return 1

    projects_by_tenant: dict[str, list[Project]] = {}
    for project in projects:
        projects_by_tenant.setdefault(project.tenant_id, []).append(project)

    max_slots = 1
    for tenant in tenants:
        tenant_policy = tenant.policy_config if isinstance(tenant.policy_config, dict) else {}
        tenant_slots = _coerce_parallel_slots(tenant_policy.get("max_concurrent_runs"))
        max_slots = max(max_slots, tenant_slots)
        for project in projects_by_tenant.get(tenant.tenant_id, []):
            project_overrides = project.policy_overrides if isinstance(project.policy_overrides, dict) else {}
            effective_policy = resolve_effective_policy(
                tenant_policy=tenant_policy,
                project_overrides=project_overrides,
            )
            project_slots = _coerce_parallel_slots(effective_policy.get("max_concurrent_runs"))
            max_slots = max(max_slots, project_slots)
    return max_slots


def _process_next_webhook_job_once(
    *,
    session_factory: sessionmaker[Session],
    owner_id: str,
) -> object | None:
    return _process_next_webhook_job_with_dependencies(
        session_factory=session_factory,
        owner_id=owner_id,
    )


def _resolve_webhook_owner_id(*, settings: Settings) -> str:
    service_instance_id = worker_service_instance_id_for_mode(settings=settings, mode=WORKER_MODE_WEBHOOKS)
    return f"worker:{service_instance_id}:child:{uuid4().hex}"


def _resolve_worker_child_capacity(
    *,
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> int:
    policy_slots = _resolve_parallel_slots_from_policy(session_factory=session_factory)
    configured_cap = _coerce_parallel_slots(getattr(settings, "worker_max_child_processes", 5))
    return max(1, min(policy_slots, configured_cap))


def _resolve_worker_child_timeout_seconds(*, settings: Settings) -> int:
    workflow_timeout_minutes = max(
        1,
        int(getattr(settings, "workflow_orchestrated_run_timeout_minutes", 90)),
    )
    return max(60, workflow_timeout_minutes * 60)


def _worker_startup_db_retry_delay_seconds(*, attempt: int) -> float:
    bounded_attempt = max(1, int(attempt))
    return min(
        WORKER_STARTUP_DB_RETRY_MAX_DELAY_SECONDS,
        WORKER_STARTUP_DB_RETRY_BASE_DELAY_SECONDS * (2 ** (bounded_attempt - 1)),
    )


def _is_retryable_worker_startup_db_error(exc: BaseException) -> bool:
    if not isinstance(exc, SQLAlchemyOperationalError):
        return False
    message = " ".join(
        part
        for part in (
            str(getattr(exc, "orig", "") or "").strip(),
            str(exc).strip(),
        )
        if part
    ).lower()
    if not message:
        return False
    return any(
        needle in message
        for needle in (
            "database system is in recovery mode",
            "connection failed",
            "could not connect",
            "connection refused",
            "server closed the connection unexpectedly",
            "the database system is starting up",
            "timeout expired",
        )
    )


def _recover_worker_run_health_once(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    agent_id: str,
    service_instance_id: str,
) -> None:
    with session_factory() as session:
        recovered = recover_stale_running_runs(
            session=session,
            settings=settings,
            recovered_by_agent_id=agent_id,
            recovered_by_service_instance_id=service_instance_id,
        )
    for item in recovered:
        logger.warning(
            "worker_stale_run_recovered run_id=%s tenant_id=%s issue_key=%s previous_owner=%s last_heartbeat_at=%s",
            item.run_id,
            item.tenant_id,
            item.issue_key,
            item.previous_owner,
            item.last_heartbeat_at.isoformat() if item.last_heartbeat_at is not None else None,
        )


def _purge_archived_tenants_once(
    *,
    session_factory: sessionmaker[Session],
) -> int:
    with session_factory() as session:
        purged = purge_expired_archived_tenants(session=session)
    if purged:
        logger.warning("worker_archived_tenant_purge_completed deleted=%s", purged)
    return purged


async def _run_stale_recovery_loop(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    stop_event: asyncio.Event,
    agent_id: str,
    service_instance_id: str,
) -> None:
    interval_seconds = max(15, int(getattr(settings, "worker_stale_sweep_interval_seconds", 60)))
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(
                _recover_worker_run_health_once,
                session_factory=session_factory,
                settings=settings,
                agent_id=agent_id,
                service_instance_id=service_instance_id,
            )
        except Exception:
            logger.exception("worker_stale_recovery_failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue


async def _run_archived_tenant_purge_loop(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    stop_event: asyncio.Event,
) -> None:
    interval_seconds = max(60, int(getattr(settings, "tenant_archive_sweep_interval_seconds", 3600)))
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(
                _purge_archived_tenants_once,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception("worker_archived_tenant_purge_failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue


async def _run_worker_runtime_heartbeat_loop(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    stop_event: asyncio.Event,
    agent_id: str,
    service_instance_id: str,
    worker_mode: str,
) -> None:
    interval_seconds = max(
        10,
        int(getattr(settings, "worker_runtime_heartbeat_interval_seconds", WORKER_RUNTIME_HEARTBEAT_INTERVAL_SECONDS)),
    )
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(
                _refresh_worker_runtime_once,
                session_factory=session_factory,
                settings=settings,
                agent_id=agent_id,
                service_instance_id=service_instance_id,
                worker_mode=worker_mode,
            )
        except Exception:
            logger.exception("worker_runtime_heartbeat_failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue


def run_worker_child_once(*, mode: str = WORKER_MODE_RUNS) -> int:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        environment=settings.sentry_environment,
        platform_version=settings.sentry_release or "dev-local",
        default_agent_id=str(settings.agent_id or "").strip() or "worker",
    )
    initialize_telemetry(
        settings=settings,
        service_name="run-worker-child" if str(mode or "").strip().lower() == WORKER_MODE_RUNS else "webhook-worker-child",
    )
    session_factory = create_session_factory()
    ensure_execution_snapshot_startup_bootstrap(
        session_factory=session_factory,
        database_url=settings.database_url,
        actor=f"worker_child:{mode}",
    )
    with session_factory() as session:
        validate_persisted_workflow_definitions(session=session)
    try:
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode == WORKER_MODE_RUNS:
            claimed_run_id = str(os.environ.get("ORCHESTRATOR_WORKER_CLAIMED_RUN_ID") or "").strip()
            claim_id = str(os.environ.get("ORCHESTRATOR_WORKER_CLAIM_ID") or "").strip()
            if not claimed_run_id or not claim_id:
                raise RuntimeError("Run worker child started without claimed run metadata")
            processed = _process_next_run_once(
                session_factory=session_factory,
                claimed_run_id=claimed_run_id,
                claim_id=claim_id,
            )
        elif normalized_mode == WORKER_MODE_WEBHOOKS:
            processed = _process_next_webhook_job_once(
                session_factory=session_factory,
                owner_id=_resolve_webhook_owner_id(settings=settings),
            )
        else:
            raise ValueError(f"Unsupported worker mode '{mode}'")
    except WorkerDependencyFailure:
        platform_metrics.record_worker_failure(kind="dependency")
        return WORKER_CHILD_EXIT_DEPENDENCY_FAILURE
    except Exception:
        platform_metrics.record_worker_failure(kind="child_crash")
        logger.exception("worker_child_failed mode=%s", mode)
        return WORKER_CHILD_EXIT_RUNTIME_FAILURE
    return WORKER_CHILD_EXIT_PROCESSED if processed is not None else WORKER_CHILD_EXIT_IDLE


async def run_worker(*, mode: str = WORKER_MODE_RUNS) -> None:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        environment=settings.sentry_environment,
        platform_version=settings.sentry_release or "dev-local",
        default_agent_id=str(settings.agent_id or "").strip() or "worker",
    )
    initialize_telemetry(
        settings=settings,
        service_name="run-worker" if str(mode or "").strip().lower() == WORKER_MODE_RUNS else "webhook-worker",
    )
    session_factory = create_session_factory()
    await asyncio.to_thread(
        ensure_execution_snapshot_startup_bootstrap,
        session_factory=session_factory,
        database_url=settings.database_url,
        actor=f"worker:{mode}",
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    if not is_postgres_database_url(settings.database_url):
        raise RuntimeError(
            "Event-driven worker requires PostgreSQL (LISTEN/NOTIFY); "
            "set ORCHESTRATOR_DATABASE_URL to a postgresql URL."
        )

    wake_event = asyncio.Event()
    listener = RunQueueNotificationBridge(
        postgres_dsn=postgres_dsn_from_database_url(settings.database_url),
        wake_event=wake_event,
        loop=loop,
        logger=logger,
        notify_channel=RUN_QUEUE_NOTIFY_CHANNEL,
        psycopg_module=psycopg,
    )
    stale_recovery_task: asyncio.Task[None] | None = None
    archived_tenant_purge_task: asyncio.Task[None] | None = None
    worker_runtime_heartbeat_task: asyncio.Task[None] | None = None
    service_instance_id = worker_service_instance_id_for_mode(settings=settings, mode=mode)
    agent_id = str(settings.agent_id or "").strip() or "worker"
    active_children: dict[asyncio.Task[WorkerChildProcessResult], WorkerChildProcessHandle] = {}
    drain_requested = True
    poll_interval_seconds = max(1, int(getattr(settings, "worker_poll_interval_seconds", 5)))
    child_timeout_seconds = _resolve_worker_child_timeout_seconds(settings=settings)
    runtime_dependency_snapshot = WorkerRuntimeDependencySnapshot(dependencies={})
    readiness_refresh_seconds = max(5, int(getattr(settings, "worker_runtime_readiness_refresh_seconds", 30)))
    auth_request_refresh_seconds = 1
    next_readiness_refresh_at: datetime | None = None
    next_auth_request_refresh_at: datetime | None = None
    runtime_block_logged_keys: tuple[str, ...] = ()
    startup_db_access_verified = False
    startup_db_retry_attempts = 0
    try:
        listener.start()
        await asyncio.to_thread(
            _register_worker_runtime_once,
            session_factory=session_factory,
            settings=settings,
            agent_id=agent_id,
            service_instance_id=service_instance_id,
            worker_mode=mode,
        )
        worker_runtime_heartbeat_task = asyncio.create_task(
            _run_worker_runtime_heartbeat_loop(
                session_factory=session_factory,
                settings=settings,
                stop_event=stop_event,
                agent_id=agent_id,
                service_instance_id=service_instance_id,
                worker_mode=mode,
            )
        )
        if mode == WORKER_MODE_RUNS:
            await asyncio.to_thread(
                prewarm_knowledge_dependencies,
                settings=settings,
            )
            await asyncio.to_thread(
                _recover_worker_run_health_once,
                session_factory=session_factory,
                settings=settings,
                agent_id=agent_id,
                service_instance_id=service_instance_id,
            )
            await asyncio.to_thread(
                _purge_archived_tenants_once,
                session_factory=session_factory,
            )
            stale_recovery_task = asyncio.create_task(
                _run_stale_recovery_loop(
                    session_factory=session_factory,
                    settings=settings,
                    stop_event=stop_event,
                    agent_id=agent_id,
                    service_instance_id=service_instance_id,
                )
            )
            archived_tenant_purge_task = asyncio.create_task(
                _run_archived_tenant_purge_loop(
                    session_factory=session_factory,
                    settings=settings,
                    stop_event=stop_event,
                )
            )
            runtime_dependency_snapshot = await asyncio.to_thread(
                _sync_run_worker_runtime_dependencies_once,
                session_factory=session_factory,
                settings=settings,
                agent_id=agent_id,
                service_instance_id=service_instance_id,
            )
            next_readiness_refresh_at = datetime.now(timezone.utc)
            next_auth_request_refresh_at = datetime.now(timezone.utc)

        logger.info("worker_started mode=%s", mode)
        while not stop_event.is_set():
            if mode == WORKER_MODE_RUNS:
                now = datetime.now(timezone.utc)
                if next_auth_request_refresh_at is None or now >= next_auth_request_refresh_at:
                    await asyncio.to_thread(
                        sync_worker_runtime_auth_requests,
                        session_factory=session_factory,
                        settings=settings,
                        service_instance_id=service_instance_id,
                    )
                    next_auth_request_refresh_at = now + timedelta(seconds=auth_request_refresh_seconds)
                if next_readiness_refresh_at is None or now >= next_readiness_refresh_at:
                    runtime_dependency_snapshot = await asyncio.to_thread(
                        _sync_run_worker_runtime_dependencies_once,
                        session_factory=session_factory,
                        settings=settings,
                        agent_id=agent_id,
                        service_instance_id=service_instance_id,
                    )
                    next_readiness_refresh_at = now + timedelta(seconds=readiness_refresh_seconds)
                blocked_runtime_kinds = tuple(sorted(runtime_dependency_snapshot.blocked_runtime_kinds))
                if blocked_runtime_kinds != runtime_block_logged_keys:
                    if blocked_runtime_kinds:
                        logger.error(
                            "worker_runtime_dependencies_degraded mode=%s blocked_runtime_kinds=%s",
                            mode,
                            ",".join(blocked_runtime_kinds),
                        )
                    else:
                        logger.info("worker_runtime_dependencies_ready mode=%s", mode)
                    runtime_block_logged_keys = blocked_runtime_kinds

            completed_children = [task for task in active_children if task.done()]
            completed_count = 0
            any_processed = False
            for task in completed_children:
                completed_count += 1
                handle = active_children.pop(task)
                try:
                    child_result = task.result()
                except Exception as exc:  # noqa: BLE001
                    platform_metrics.record_worker_failure(kind="child_crash")
                    logger.exception("worker_child_task_failed mode=%s error=%s", mode, exc)
                    drain_requested = True
                    continue
                logger.info(
                    "worker_child_completed mode=%s pid=%s return_code=%s processed=%s dependency_failure=%s timed_out=%s",
                    mode,
                    handle.process.pid,
                    child_result.return_code,
                    child_result.processed,
                    child_result.dependency_failure,
                    child_result.timed_out,
                )
                if child_result.dependency_failure:
                    raise WorkerDependencyFailure("Worker runtime unavailable in child process")
                if child_result.return_code == WORKER_CHILD_EXIT_RUNTIME_FAILURE:
                    platform_metrics.record_worker_failure(kind="child_crash")
                    logger.error(
                        "worker_child_failed mode=%s return_code=%s pid=%s timed_out=%s",
                        mode,
                        child_result.return_code,
                        handle.process.pid,
                        child_result.timed_out,
                    )
                    drain_requested = True
                    continue
                if child_result.return_code not in {WORKER_CHILD_EXIT_PROCESSED, WORKER_CHILD_EXIT_IDLE}:
                    platform_metrics.record_worker_failure(kind="child_crash")
                    logger.error(
                        "worker_child_unexpected_exit mode=%s return_code=%s pid=%s",
                        mode,
                        child_result.return_code,
                        handle.process.pid,
                    )
                    drain_requested = True
                    continue
                if child_result.processed:
                    if (
                        mode == WORKER_MODE_RUNS
                        and handle.claimed_run_id
                        and handle.claim_id
                        and handle.worker_service_instance_id
                    ):
                        post_child_status = await asyncio.to_thread(
                            _reconcile_claimed_run_after_child_exit,
                            session_factory=session_factory,
                            run_id=handle.claimed_run_id,
                            claim_id=handle.claim_id,
                            worker_service_instance_id=handle.worker_service_instance_id,
                        )
                        if post_child_status not in _VALID_POST_CHILD_RUN_STATUSES:
                            platform_metrics.record_worker_failure(kind="child_crash")
                            logger.error(
                                "worker_child_left_illegal_run_state mode=%s pid=%s run_id=%s status=%s",
                                mode,
                                handle.process.pid,
                                handle.claimed_run_id,
                                post_child_status,
                            )
                            drain_requested = True
                            continue
                    any_processed = True

            if any_processed:
                drain_requested = True
            elif completed_count > 0 and not active_children and not wake_event.is_set():
                drain_requested = False

            if wake_event.is_set():
                wake_event.clear()
                drain_requested = True

            if stop_event.is_set():
                break

            parallel_slots = _resolve_worker_child_capacity(
                settings=settings,
                session_factory=session_factory,
            )
            while drain_requested and len(active_children) < parallel_slots and not stop_event.is_set():
                try:
                    if mode == WORKER_MODE_RUNS:
                        claimed_run = await asyncio.to_thread(
                            _claim_next_run_once,
                            session_factory=session_factory,
                            settings=settings,
                            service_instance_id=service_instance_id,
                            ready_runtime_kinds=runtime_dependency_snapshot.ready_runtime_kinds,
                        )
                        if claimed_run is None:
                            run_probe = await asyncio.to_thread(
                                _probe_claimable_run_once,
                                session_factory=session_factory,
                                settings=settings,
                                ready_runtime_kinds=runtime_dependency_snapshot.ready_runtime_kinds,
                            )
                            startup_db_access_verified = True
                            startup_db_retry_attempts = 0
                            logger.info(
                                "worker_no_claimable_run reason=%s run_id=%s tenant_id=%s issue_key=%s",
                                run_probe.reason.value,
                                run_probe.run_id,
                                run_probe.tenant_id,
                                run_probe.issue_key,
                            )
                            drain_requested = False
                            break
                        startup_db_access_verified = True
                        startup_db_retry_attempts = 0
                        logger.info(
                            "worker_claimed_run_for_child run_id=%s tenant_id=%s issue_key=%s worker_service_instance_id=%s claim_id=%s",
                            claimed_run.run_id,
                            claimed_run.tenant_id,
                            claimed_run.issue_key,
                            service_instance_id,
                            claimed_run.claim_id,
                        )
                    else:
                        has_webhook_job = await asyncio.to_thread(
                            _has_available_webhook_job_once,
                            session_factory=session_factory,
                        )
                        startup_db_access_verified = True
                        startup_db_retry_attempts = 0
                        if not has_webhook_job:
                            drain_requested = False
                            break
                except Exception as exc:
                    if (
                        not startup_db_access_verified
                        and _is_retryable_worker_startup_db_error(exc)
                    ):
                        startup_db_retry_attempts += 1
                        retry_delay_seconds = _worker_startup_db_retry_delay_seconds(
                            attempt=startup_db_retry_attempts
                        )
                        logger.warning(
                            "worker_startup_database_unavailable mode=%s attempt=%s max_attempts=%s retry_in_seconds=%.1f error=%s",
                            mode,
                            startup_db_retry_attempts,
                            WORKER_STARTUP_DB_RETRY_MAX_ATTEMPTS,
                            retry_delay_seconds,
                            exc,
                        )
                        if startup_db_retry_attempts >= WORKER_STARTUP_DB_RETRY_MAX_ATTEMPTS:
                            logger.exception(
                                "worker_startup_database_retry_exhausted mode=%s attempts=%s",
                                mode,
                                startup_db_retry_attempts,
                            )
                            raise
                        await wait_for_wake_or_stop(
                            wake_event=wake_event,
                            stop_event=stop_event,
                            timeout_seconds=retry_delay_seconds,
                        )
                        drain_requested = True
                        continue
                    raise
                child = await _spawn_worker_child_process(
                    mode=mode,
                    wake_event=wake_event,
                    child_timeout_seconds=child_timeout_seconds,
                    claimed_run_id=claimed_run.run_id if mode == WORKER_MODE_RUNS else None,
                    claim_id=claimed_run.claim_id if mode == WORKER_MODE_RUNS else None,
                    worker_service_instance_id=(
                        claimed_run.worker_service_instance_id if mode == WORKER_MODE_RUNS else None
                    ),
                )
                active_children[child.wait_task] = child

            if stop_event.is_set():
                break

            if not active_children and not drain_requested:
                timed_out = await wait_for_wake_or_stop(
                    wake_event=wake_event,
                    stop_event=stop_event,
                    timeout_seconds=float(poll_interval_seconds),
                )
                if timed_out and not stop_event.is_set():
                    drain_requested = True
                continue

            if active_children:
                wake_task = asyncio.create_task(
                    wait_for_wake_or_stop(
                        wake_event=wake_event,
                        stop_event=stop_event,
                        timeout_seconds=float(poll_interval_seconds),
                    )
                )
                done, pending = await asyncio.wait(
                    set(active_children.keys()) | {wake_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if wake_task in done:
                    with suppress(asyncio.CancelledError):
                        wake_task.result()
                else:
                    wake_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await wake_task
                continue
    except WorkerDependencyFailure:
        raise
    except Exception:
        platform_metrics.record_worker_failure(kind="crash")
        raise
    finally:
        await _terminate_worker_child_processes(active_children=active_children)
        with suppress(Exception):
            await asyncio.to_thread(stop_all_live_runtime_auth_sessions)
        if stale_recovery_task is not None:
            stale_recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await stale_recovery_task
        if archived_tenant_purge_task is not None:
            archived_tenant_purge_task.cancel()
            with suppress(asyncio.CancelledError):
                await archived_tenant_purge_task
        if worker_runtime_heartbeat_task is not None:
            worker_runtime_heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_runtime_heartbeat_task
        with suppress(Exception):
            await asyncio.to_thread(
                _stop_worker_runtime_once,
                session_factory=session_factory,
                settings=settings,
                agent_id=agent_id,
                service_instance_id=service_instance_id,
                worker_mode=mode,
            )
        listener.stop()
        logger.info("worker_stopped mode=%s", mode)


def main(*, mode: str = WORKER_MODE_RUNS) -> None:
    asyncio.run(run_worker(mode=mode))


if __name__ == "__main__":
    main()
