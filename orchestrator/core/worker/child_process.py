from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import suppress
from dataclasses import dataclass

WORKER_CHILD_EXIT_PROCESSED = 0
WORKER_CHILD_EXIT_IDLE = 3
WORKER_CHILD_EXIT_DEPENDENCY_FAILURE = 4
WORKER_CHILD_EXIT_RUNTIME_FAILURE = 5
WORKER_CHILD_EXIT_TRANSIENT_FAILURE = 6

logger = logging.getLogger(__name__)


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
    claimed_run_id: str | None = None
    claim_id: str | None = None
    worker_service_instance_id: str | None = None


def child_command_for_mode(*, mode: str) -> str:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode == "runs":
        return "worker-child-runs"
    if normalized_mode == "webhooks":
        return "worker-child-webhooks"
    raise ValueError(f"Unsupported worker mode '{mode}'")


async def spawn_worker_child_process(
    *,
    mode: str,
    wake_event: asyncio.Event,
    child_timeout_seconds: int,
    claimed_run_id: str | None = None,
    claim_id: str | None = None,
    worker_service_instance_id: str | None = None,
) -> WorkerChildProcessHandle:
    _ = wake_event
    child_env = None
    if claimed_run_id:
        child_env = os.environ.copy()
        child_env["ORCHESTRATOR_WORKER_CLAIMED_RUN_ID"] = claimed_run_id
        child_env["ORCHESTRATOR_WORKER_CLAIM_ID"] = str(claim_id or "").strip()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "orchestrator",
        child_command_for_mode(mode=mode),
        env=child_env,
    )
    logger.info(
        "worker_child_spawned mode=%s pid=%s timeout_seconds=%s run_id=%s",
        mode,
        process.pid,
        child_timeout_seconds,
        claimed_run_id,
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
    return WorkerChildProcessHandle(
        process=process,
        wait_task=wait_task,
        claimed_run_id=claimed_run_id,
        claim_id=claim_id,
        worker_service_instance_id=worker_service_instance_id,
    )


async def terminate_worker_child_processes(
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
