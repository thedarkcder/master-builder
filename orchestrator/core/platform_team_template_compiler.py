from __future__ import annotations

from collections import deque
from dataclasses import dataclass


def _clean_text(value: object | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _clean_key(value: object | None, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


@dataclass(frozen=True)
class CompiledPlatformTeamRole:
    role_key: str
    label: str
    description: str | None
    position: int
    persona_key: str
    agent_key: str


@dataclass(frozen=True)
class CompiledPlatformTeamTask:
    task_key: str
    label: str
    owner_role_key: str
    position: int
    executor_kind: str | None
    artifact_contract: dict
    approval_rule: dict


@dataclass(frozen=True)
class CompiledPlatformTeamEdge:
    from_task_key: str
    to_task_key: str


@dataclass(frozen=True)
class CompiledPlatformTeamTemplate:
    roles: list[CompiledPlatformTeamRole]
    tasks: list[CompiledPlatformTeamTask]
    edges: list[CompiledPlatformTeamEdge]


def _validate_task_graph(*, task_keys: set[str], edges: list[CompiledPlatformTeamEdge]) -> None:
    if not task_keys:
        raise ValueError("Team template must define at least one task")

    inbound_count = {task_key: 0 for task_key in task_keys}
    adjacency: dict[str, list[str]] = {task_key: [] for task_key in task_keys}
    seen_edges: set[tuple[str, str]] = set()
    for edge in edges:
        edge_key = (edge.from_task_key, edge.to_task_key)
        if edge_key in seen_edges:
            raise ValueError(f"Duplicate edge: {edge.from_task_key}->{edge.to_task_key}")
        seen_edges.add(edge_key)
        inbound_count[edge.to_task_key] += 1
        adjacency[edge.from_task_key].append(edge.to_task_key)

    roots = [task_key for task_key, inbound in inbound_count.items() if inbound == 0]
    if not roots:
        raise ValueError("Team template must include at least one root task; dependency graph must be acyclic")

    remaining_inbound = dict(inbound_count)
    ready = deque(sorted(roots))
    visited = 0
    while ready:
        task_key = ready.popleft()
        visited += 1
        for successor in adjacency[task_key]:
            remaining_inbound[successor] -= 1
            if remaining_inbound[successor] == 0:
                ready.append(successor)

    if visited != len(task_keys):
        raise ValueError("Team template dependency graph must be acyclic")


def compile_platform_team_template(payload: dict) -> CompiledPlatformTeamTemplate:
    role_keys: set[str] = set()
    compiled_roles: list[CompiledPlatformTeamRole] = []
    for raw_role in payload.get("roles") or []:
        role_key = _clean_key(raw_role.get("role_key"), field_name="role_key")
        if role_key in role_keys:
            raise ValueError(f"Duplicate role_key: {role_key}")
        role_keys.add(role_key)
        compiled_roles.append(
            CompiledPlatformTeamRole(
                role_key=role_key,
                label=_clean_key(raw_role.get("label"), field_name="label"),
                description=_clean_text(raw_role.get("description")),
                position=max(1, int(raw_role.get("position") or 1)),
                persona_key=_clean_key(raw_role.get("persona_key"), field_name="persona_key"),
                agent_key=_clean_key(raw_role.get("agent_key"), field_name="agent_key"),
            )
        )

    task_keys: set[str] = set()
    compiled_tasks: list[CompiledPlatformTeamTask] = []
    for raw_task in payload.get("tasks") or []:
        task_key = _clean_key(raw_task.get("task_key"), field_name="task_key")
        if task_key in task_keys:
            raise ValueError(f"Duplicate task_key: {task_key}")
        owner_role_key = _clean_key(raw_task.get("owner_role_key"), field_name="owner_role_key")
        if owner_role_key not in role_keys:
            raise ValueError(f"Task {task_key} references unknown role: {owner_role_key}")
        task_keys.add(task_key)
        compiled_tasks.append(
            CompiledPlatformTeamTask(
                task_key=task_key,
                label=_clean_key(raw_task.get("label"), field_name="label"),
                owner_role_key=owner_role_key,
                position=max(1, int(raw_task.get("position") or 1)),
                executor_kind=_clean_text(raw_task.get("executor_kind")),
                artifact_contract=dict(raw_task.get("artifact_contract") or {}),
                approval_rule=dict(raw_task.get("approval_rule") or {}),
            )
        )

    compiled_edges: list[CompiledPlatformTeamEdge] = []
    for raw_edge in payload.get("edges") or []:
        from_task_key = _clean_key(raw_edge.get("from_task_key"), field_name="from_task_key")
        to_task_key = _clean_key(raw_edge.get("to_task_key"), field_name="to_task_key")
        if from_task_key not in task_keys or to_task_key not in task_keys:
            raise ValueError("Edges must reference existing task keys")
        compiled_edges.append(
            CompiledPlatformTeamEdge(
                from_task_key=from_task_key,
                to_task_key=to_task_key,
            )
        )

    _validate_task_graph(task_keys=task_keys, edges=compiled_edges)

    return CompiledPlatformTeamTemplate(
        roles=compiled_roles,
        tasks=compiled_tasks,
        edges=compiled_edges,
    )
