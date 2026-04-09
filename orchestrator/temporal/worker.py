from __future__ import annotations

import asyncio

from orchestrator.core.config import get_settings
from orchestrator.temporal.client import connect_temporal_client, temporal_task_queue


async def _run_temporal_worker_async() -> None:
    from temporalio.worker import Worker
    from orchestrator.temporal.activities.team_run import (
        complete_team_task_activity,
        execute_ready_team_task_activity,
        initialize_team_run_activity,
        resume_team_human_input_activity,
        submit_team_approval_activity,
    )
    from orchestrator.temporal.workflows.team_run import TeamRunWorkflow

    settings = get_settings()
    client = await connect_temporal_client(settings)
    worker = Worker(
        client,
        task_queue=temporal_task_queue(settings),
        workflows=[TeamRunWorkflow],
        activities=[
            initialize_team_run_activity,
            execute_ready_team_task_activity,
            complete_team_task_activity,
            submit_team_approval_activity,
            resume_team_human_input_activity,
        ],
    )
    await worker.run()


def run_temporal_worker() -> int:
    asyncio.run(_run_temporal_worker_async())
    return 0
