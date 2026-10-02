from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestrator.core.deployment_host_queue import (
    fail_stale_running_deployment_host_commands,
)
from orchestrator.storage.models import (
    DeploymentHostCommand,
    ProjectDeploymentRestoreRun,
)


def fail_stale_running_restore_commands(
    session: Session,
    *,
    host_id: str | None = None,
    now: datetime | None = None,
) -> list[DeploymentHostCommand]:
    timestamp = now or datetime.now(timezone.utc)
    stale_commands = fail_stale_running_deployment_host_commands(
        session=session, host_id=host_id, now=timestamp
    )
    for command in stale_commands:
        if command.kind != "restore_database" or not command.restore_run_id:
            continue
        restore_run = session.get(ProjectDeploymentRestoreRun, command.restore_run_id)
        if restore_run is None:
            continue
        restore_run.status = "failed"
        restore_run.last_error = command.last_error
        restore_run.completed_at = timestamp
        restore_run.updated_at = timestamp
    if stale_commands:
        session.flush()
    return stale_commands
