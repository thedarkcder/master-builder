from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.config import get_settings
from orchestrator.core.logging import configure_logging
from orchestrator.core.workflow_runner import WorkflowRequest, WorkflowRunner
from orchestrator.storage.models import Run, Tenant

logger = logging.getLogger(__name__)

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"


def _workflow_request_for_run(tenant: Tenant, run: Run) -> WorkflowRequest:
    max_loops = int(tenant.policy_config.get("max_dev_test_review_loops", 1))
    suggested_test_commands = tenant.policy_config.get("allowed_commands") or []
    return WorkflowRequest(
        tenant_id=tenant.tenant_id,
        run_id=run.run_id,
        issue_key=run.issue_key,
        issue_summary=f"Execute {run.issue_key}",
        issue_description="",
        max_dev_test_review_loops=max_loops,
        suggested_test_commands=[str(command) for command in suggested_test_commands],
    )


def process_next_queued_run(session: Session, runner: WorkflowRunner) -> Run | None:
    run = session.execute(
        select(Run).where(Run.status == RUN_STATUS_QUEUED).order_by(Run.created_at.asc())
    ).scalar_one_or_none()
    if run is None:
        return None

    tenant = session.get(Tenant, run.tenant_id)
    if tenant is None:
        run.status = RUN_STATUS_FAILED
        run.last_error = "Tenant not found for queued run"
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(run)
        return run

    run.status = RUN_STATUS_RUNNING
    run.started_at = datetime.now(timezone.utc)
    session.commit()

    workflow_result = runner.run(_workflow_request_for_run(tenant, run))
    run.plan = workflow_result.to_plan_payload()
    run.pr_url = workflow_result.pr_url
    run.finished_at = datetime.now(timezone.utc)
    if workflow_result.succeeded:
        run.status = RUN_STATUS_SUCCEEDED
        run.last_error = None
    else:
        run.status = RUN_STATUS_FAILED
        if workflow_result.diagnostics is not None:
            run.last_error = workflow_result.diagnostics.message
        else:
            run.last_error = "Workflow failed without diagnostics"

    session.commit()
    session.refresh(run)
    return run


async def run_worker() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info("worker_started")
    await stop_event.wait()
    logger.info("worker_stopped")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
