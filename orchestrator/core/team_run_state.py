from __future__ import annotations

from copy import deepcopy
from typing import Any

from orchestrator.core.runs import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_WAITING_FOR_INPUT,
    RunStateTransitionError,
)


def team_run_nodes(team_run: dict[str, Any]) -> list[dict[str, Any]]:
    return [node for node in team_run.get("nodes", []) if isinstance(node, dict)]


def team_run_node(team_run: dict[str, Any], task_key: str) -> dict[str, Any]:
    normalized_task_key = str(task_key or "").strip()
    for node in team_run_nodes(team_run):
        if str(node.get("task_key") or "").strip() == normalized_task_key:
            return node
    raise RunStateTransitionError(f"Team task not found: {task_key}")


def set_team_run_node_status(
    team_run: dict[str, Any],
    task_key: str,
    status: str,
    *,
    attempt: int | None = None,
    summary: str | None = None,
) -> None:
    node = team_run_node(team_run, task_key)
    node["status"] = status
    if attempt is not None:
        node["attempt"] = int(attempt)
    if summary is not None:
        node["summary"] = summary


def get_team_run_runtime_state(team_run: dict[str, Any]) -> dict[str, Any]:
    raw = team_run.get("runtime_state")
    return deepcopy(raw) if isinstance(raw, dict) else {}


def set_team_run_runtime_state(team_run: dict[str, Any], runtime_state: dict[str, Any]) -> None:
    team_run["runtime_state"] = deepcopy(runtime_state)


def _dependencies_complete(team_run: dict[str, Any], task_key: str) -> bool:
    status_by_task = {
        str(node.get("task_key") or "").strip(): str(node.get("status") or "").strip().lower()
        for node in team_run_nodes(team_run)
    }
    node = team_run_node(team_run, task_key)
    for dependency_key in node.get("dependency_keys", []):
        if status_by_task.get(str(dependency_key or "").strip()) != "completed":
            return False
    return True


def promote_ready_team_run_nodes(team_run: dict[str, Any]) -> None:
    for node in team_run_nodes(team_run):
        status = str(node.get("status") or "").strip().lower()
        if status != "pending":
            continue
        task_key = str(node.get("task_key") or "").strip()
        if task_key and _dependencies_complete(team_run, task_key):
            node["status"] = "ready"


def derive_team_run_status(team_run: dict[str, Any]) -> tuple[str, str, str | None]:
    statuses = {str(node.get("status") or "").strip().lower() for node in team_run_nodes(team_run)}
    if "blocked" in statuses:
        return RUN_STATUS_BLOCKED, RUN_STATUS_BLOCKED, "Team run blocked by task or approval rejection"
    if "waiting_for_input" in statuses:
        return RUN_STATUS_WAITING_FOR_INPUT, RUN_STATUS_WAITING_FOR_INPUT, None
    if "failed" in statuses:
        return RUN_STATUS_FAILED, RUN_STATUS_FAILED, "Team run failed"
    if statuses and statuses.issubset({"completed"}):
        return RUN_STATUS_SUCCEEDED, RUN_STATUS_SUCCEEDED, None
    return RUN_STATUS_RUNNING, RUN_STATUS_RUNNING, None


def first_ready_auto_team_task(team_run: dict[str, Any]) -> dict[str, Any] | None:
    for node in team_run_nodes(team_run):
        if str(node.get("status") or "").strip().lower() != "ready":
            continue
        if str(node.get("executor_kind") or "").strip():
            return node
    return None
