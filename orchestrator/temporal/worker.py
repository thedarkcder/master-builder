from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from orchestrator.core.config import get_settings
from orchestrator.temporal.client import connect_temporal_client, temporal_task_queue


async def run_temporal_worker() -> None:
    try:  # pragma: no cover - exercised when temporal backend is enabled
        from temporalio.worker import Worker
    except ImportError as exc:  # pragma: no cover - exercised when temporal backend is enabled
        raise RuntimeError("Temporal backend requires temporalio to be installed") from exc

    from orchestrator.temporal.activities.run_execution import (
        execute_claimed_run_activity,
        resume_human_input_activity,
    )
    from orchestrator.temporal.workflows.development_team_run import DevelopmentTeamRunWorkflow

    settings = get_settings()
    client = await connect_temporal_client(settings)
    worker = Worker(
        client,
        task_queue=temporal_task_queue(settings),
        workflows=[DevelopmentTeamRunWorkflow],
        activities=[execute_claimed_run_activity, resume_human_input_activity],
        activity_executor=ThreadPoolExecutor(max_workers=4),
    )
    await worker.run()
