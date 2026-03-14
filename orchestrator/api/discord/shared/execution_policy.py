from __future__ import annotations

from fastapi import HTTPException, status
from orchestrator.storage.models import Tenant


def ensure_issue_is_executable(
    *,
    issue_status: str,
    tenant: Tenant,
    normalize_status_name_fn,
    extra_executable_statuses: list[str] | tuple[str, ...] | None = None,
) -> None:  # noqa: ANN001
    executable_statuses = ["To Do"]
    configured_ready_statuses = tenant.jira_config.get("ready_statuses")
    if isinstance(configured_ready_statuses, list):
        executable_statuses.extend(
            status_name.strip()
            for status_name in (str(value) for value in configured_ready_statuses)
            if status_name.strip()
        )
    if extra_executable_statuses:
        executable_statuses.extend(
            status_name.strip()
            for status_name in (str(value) for value in extra_executable_statuses)
            if status_name.strip()
        )
    normalized_executable_statuses = {normalize_status_name_fn(value) for value in executable_statuses}
    if normalize_status_name_fn(issue_status) not in normalized_executable_statuses:
        display_statuses = ", ".join(sorted(set(executable_statuses)))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Issue is in '{issue_status}', expected one of: {display_statuses}",
        )
