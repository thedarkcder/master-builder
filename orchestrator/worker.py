from __future__ import annotations

import asyncio
import logging
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.api.admin.tenant_crud import purge_expired_archived_tenants
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.config import Settings, get_settings
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.logging import configure_logging
from orchestrator.core.knowledge_prewarm import prewarm_knowledge_dependencies
from orchestrator.core.platform_metrics import platform_metrics
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.runs import RUN_STATUS_DISPATCHING, RUN_STATUS_RUNNING
from orchestrator.core.worker.run_health import (
    recover_stale_running_runs,
    worker_service_instance_id_for_mode,
)
from orchestrator.core.worker.queue_selector import (
    QueueClaimabilityProbe,
    probe_claimable_queued_run,
)
from orchestrator.core.worker.execution_service import (
    process_next_webhook_job_with_dependencies as _process_next_webhook_job_with_dependencies,
    process_next_queued_run_with_dependencies as _process_next_queued_run_with_dependencies,
)
from orchestrator.core.worker.queue_listener import (
    RunQueueNotificationBridge,
    wait_for_wake_or_stop,
)
from orchestrator.core.worker.runtime_factory import build_workflow_runner_for_session
from orchestrator.core.worker_capabilities import resolve_worker_capability_context
from orchestrator.core.workflow.runner import WorkflowRunner
from orchestrator.core.workflow.execution_snapshot_startup import ensure_execution_snapshot_startup_bootstrap
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.run_queue_events import (
    RUN_QUEUE_NOTIFY_CHANNEL,
    is_postgres_database_url,
    postgres_dsn_from_database_url,
)
from orchestrator.storage.models import Project, Run, Tenant, WebhookJob, WorkerRuntimeState

try:
    import psycopg
except ImportError:  # pragma: no cover - dependency is required at runtime
    psycopg = None

logger = logging.getLogger(__name__)
WORKER_RUNTIME_HEARTBEAT_INTERVAL_SECONDS = 30
WORKER_MODE_RUNS = "runs"
WORKER_MODE_WEBHOOKS = "webhooks"
WORKER_CHILD_EXIT_PROCESSED = 0
WORKER_CHILD_EXIT_IDLE = 3
WORKER_CHILD_EXIT_DEPENDENCY_FAILURE = 4
WORKER_CHILD_EXIT_RUNTIME_FAILURE = 5


class WorkerDependencyFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerChildProcessResult:
    return_code: int
    processed: bool
    dependency_failure: bool
    timed_out: bool = False


@dataclass(frozen=True)
class WorkerChildProcessHandle:
    process: asyncio.subprocess.Process
    wait_task: asyncio.Task[WorkerChildProcessResult]


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
) -> None:
    now = datetime.now(timezone.utc)
    capabilities = _worker_runtime_capabilities(settings=settings)
    with session_factory() as session:
        row = session.get(WorkerRuntimeState, service_instance_id)
        if row is None:
            row = WorkerRuntimeState(
                service_instance_id=service_instance_id,
                agent_id=agent_id,
                worker_mode=worker_mode,
                capabilities_json=capabilities,
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
    state = "busy" if active_run_count > 0 else "idle"
    _upsert_worker_runtime_state_once(
        session_factory=session_factory,
        settings=settings,
        agent_id=agent_id,
        service_instance_id=service_instance_id,
        worker_mode=worker_mode,
        state=state,
    )


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


def process_next_queued_run(session: Session, runner: WorkflowRunner) -> object | None:
    return _process_next_queued_run_with_dependencies(
        session=session,
        runner=runner,
        send_discord_message_fn=send_tenant_discord_message,
    )


def _process_next_webhook_job_once(
    *,
    session_factory: sessionmaker[Session],
    owner_id: str,
) -> object | None:
    return _process_next_webhook_job_with_dependencies(
        session_factory=session_factory,
        owner_id=owner_id,
    )


def _process_next_run_once(*, session_factory: sessionmaker[Session]) -> object | None:
    class _LazyWorkflowRunner:
        def __init__(self, *, session: Session) -> None:
            self._session = session
            self._runner: WorkflowRunner | None = None

        def run(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            if self._runner is None:
                try:
                    self._runner = build_workflow_runner_for_session(session=self._session)
                except CodexRuntimeError as exc:
                    platform_metrics.record_worker_failure(kind="dependency")
                    raise WorkerDependencyFailure(f"Worker runtime unavailable: {exc}") from exc
            return self._runner.run(*args, **kwargs)

    with session_factory() as session:
        result = process_next_queued_run(session, _LazyWorkflowRunner(session=session))
        if result is None or isinstance(result, Run):
            return result
        logger.warning(
            "worker_run_child_non_run_result type=%s",
            type(result).__name__,
        )
        return None


def _resolve_webhook_owner_id(*, settings: Settings) -> str:
    service_instance_id = worker_service_instance_id_for_mode(settings=settings, mode=WORKER_MODE_WEBHOOKS)
    return f"worker:{service_instance_id}:child:{uuid4().hex}"


def _child_command_for_mode(*, mode: str) -> str:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode == WORKER_MODE_RUNS:
        return "worker-child-runs"
    if normalized_mode == WORKER_MODE_WEBHOOKS:
        return "worker-child-webhooks"
    raise ValueError(f"Unsupported worker mode '{mode}'")


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


def _probe_claimable_run_once(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> QueueClaimabilityProbe:
    capability_context = resolve_worker_capability_context(
        raw_value=getattr(settings, "worker_capabilities", None),
        source="ORCHESTRATOR_WORKER_CAPABILITIES",
    )
    with session_factory() as session:
        return probe_claimable_queued_run(
            session,
            queued_status="queued",
            running_status="running",
            worker_capabilities=set(capability_context.available),
            running_stale_timeout_seconds=max(
                60,
                int(getattr(settings, "worker_run_stale_timeout_seconds", 300)),
            ),
        )


def _has_available_webhook_job_once(
    *,
    session_factory: sessionmaker[Session],
) -> bool:
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        job_id = session.execute(
            select(WebhookJob.job_id)
            .where(
                WebhookJob.available_at <= now,
                or_(
                    WebhookJob.status == "pending",
                    and_(
                        WebhookJob.status == "processing",
                        WebhookJob.lease_expires_at.is_not(None),
                        WebhookJob.lease_expires_at <= now,
                    ),
                ),
            )
            .order_by(WebhookJob.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        return job_id is not None


async def _spawn_worker_child_process(
    *,
    mode: str,
    wake_event: asyncio.Event,
    child_timeout_seconds: int,
) -> WorkerChildProcessHandle:
    _ = wake_event
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "orchestrator.cli",
        _child_command_for_mode(mode=mode),
    )
    logger.info(
        "worker_child_spawned mode=%s pid=%s timeout_seconds=%s",
        mode,
        process.pid,
        child_timeout_seconds,
    )

    async def _await_result() -> WorkerChildProcessResult:
        try:
            return_code = await asyncio.wait_for(
                process.wait(),
                timeout=max(1, int(child_timeout_seconds)),
            )
        except asyncio.TimeoutError:
            logger.error(
                "worker_child_timed_out mode=%s pid=%s timeout_seconds=%s",
                mode,
                process.pid,
                child_timeout_seconds,
            )
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                return_code = await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.error(
                    "worker_child_terminate_grace_expired mode=%s pid=%s",
                    mode,
                    process.pid,
                )
                with suppress(ProcessLookupError):
                    process.kill()
                return_code = await process.wait()
            return WorkerChildProcessResult(
                return_code=WORKER_CHILD_EXIT_RUNTIME_FAILURE,
                processed=False,
                dependency_failure=False,
                timed_out=True,
            )
        return WorkerChildProcessResult(
            return_code=return_code,
            processed=return_code == WORKER_CHILD_EXIT_PROCESSED,
            dependency_failure=return_code == WORKER_CHILD_EXIT_DEPENDENCY_FAILURE,
        )

    wait_task = asyncio.create_task(_await_result())
    return WorkerChildProcessHandle(process=process, wait_task=wait_task)


async def _terminate_worker_child_processes(
    *,
    active_children: dict[asyncio.Task[WorkerChildProcessResult], WorkerChildProcessHandle],
) -> None:
    handles = list(active_children.values())
    for handle in handles:
        if handle.process.returncode is not None:
            continue
        with suppress(ProcessLookupError):
            handle.process.terminate()
    waiters: list[asyncio.Task[WorkerChildProcessResult]] = [handle.wait_task for handle in handles]
    if waiters:
        await asyncio.gather(*waiters, return_exceptions=True)


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
    session_factory = create_session_factory()
    ensure_execution_snapshot_startup_bootstrap(
        session_factory=session_factory,
        database_url=settings.database_url,
        actor=f"worker_child:{mode}",
    )
    try:
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode == WORKER_MODE_RUNS:
            processed = _process_next_run_once(session_factory=session_factory)
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

        logger.info("worker_started mode=%s", mode)
        while not stop_event.is_set():
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
                if mode == WORKER_MODE_RUNS:
                    run_probe = await asyncio.to_thread(
                        _probe_claimable_run_once,
                        session_factory=session_factory,
                        settings=settings,
                    )
                    if not run_probe.claimable:
                        logger.info(
                            "worker_no_claimable_run reason=%s run_id=%s tenant_id=%s issue_key=%s",
                            run_probe.reason.value,
                            run_probe.run_id,
                            run_probe.tenant_id,
                            run_probe.issue_key,
                        )
                        drain_requested = False
                        break
                else:
                    has_webhook_job = await asyncio.to_thread(
                        _has_available_webhook_job_once,
                        session_factory=session_factory,
                    )
                    if not has_webhook_job:
                        drain_requested = False
                        break
                child = await _spawn_worker_child_process(
                    mode=mode,
                    wake_event=wake_event,
                    child_timeout_seconds=child_timeout_seconds,
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
