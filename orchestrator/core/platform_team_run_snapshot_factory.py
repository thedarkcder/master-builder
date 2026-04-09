from __future__ import annotations

from copy import deepcopy
from typing import Callable

from sqlalchemy.orm import Session

from orchestrator.core.issue_workflow_contract import (
    ISSUE_WORKFLOW_LOOP_STAGE_KEYS,
    ISSUE_WORKFLOW_STAGE_KEYS,
    ISSUE_WORKFLOW_STAGE_TO_EXECUTOR_KIND,
)
from orchestrator.core.platform_catalog_projection_service import platform_catalog_projection_service
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot, ExecutionStageRecord
from orchestrator.storage.models import PlatformTeamTemplate


def _clean_text(value: object | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _clean_key(value: object | None, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _clean_string_list(value: object | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in (_clean_text(entry) for entry in value) if item]


def _status_from_stage_record(record: ExecutionStageRecord | None) -> str:
    if record is None:
        return "pending"
    normalized = str(record.status or "").strip().lower()
    if normalized in {"completed", "approved"}:
        return "completed"
    if normalized in {"running", "started"}:
        return "running"
    if normalized in {"waiting_for_input", "requeue", "blocked", "failed", "interrupted"}:
        return normalized
    return "pending"


def _issue_runtime_state_from_snapshot(
    *,
    snapshot: ExecutionSnapshot,
    entry_mode: str | None,
    entry_stage: str | None,
    max_loops: int | None = None,
) -> dict[str, object]:
    existing_team_run = snapshot.context.execution_context.get("team_run")
    if isinstance(existing_team_run, dict):
        runtime_state = existing_team_run.get("runtime_state")
        if isinstance(runtime_state, dict):
            return deepcopy(runtime_state)
    attempts = [
        int(record.attempt)
        for stage_name, record in snapshot.stages.items()
        if stage_name in ISSUE_WORKFLOW_LOOP_STAGE_KEYS and isinstance(record, ExecutionStageRecord)
    ]
    return {
        "execution_kind": "issue_workflow",
        "entry_mode": str(entry_mode or "").strip().lower() or "fresh",
        "entry_stage": str(entry_stage or "").strip().lower() or None,
        "attempt": max(attempts or [1]),
        "max_attempts": max(1, int(max_loops or 1)),
        "next_feedback": None,
        "history": [],
        "stage_trace": [dict(item) for item in snapshot.events.stage_trace if isinstance(item, dict)],
        "test_guidance": [],
        "dev_rationale": [],
        "review_summary": [],
        "review_feedback": None,
    }


def _issue_node_statuses(
    *,
    snapshot: ExecutionSnapshot,
    entry_mode: str | None,
    entry_stage: str | None,
) -> dict[str, str]:
    statuses = {
        stage_key: _status_from_stage_record(snapshot.stages.get(stage_key))
        for stage_key in ISSUE_WORKFLOW_STAGE_KEYS
    }
    normalized_entry_mode = str(entry_mode or "").strip().lower()
    normalized_entry_stage = str(entry_stage or "").strip().lower()
    if normalized_entry_mode == "resume":
        if normalized_entry_stage == "pm":
            return {"pm": "ready", "dev": "pending", "test": "pending", "review": "pending"}
        if normalized_entry_stage == "dev":
            return {"pm": "completed", "dev": "ready", "test": "pending", "review": "pending"}
        if normalized_entry_stage == "review":
            return {"pm": "completed", "dev": "completed", "test": "completed", "review": "ready"}

    if statuses["pm"] == "pending":
        statuses["pm"] = "ready"
        statuses["dev"] = "pending"
        statuses["test"] = "pending"
        statuses["review"] = "pending"
        return statuses

    if statuses["pm"] != "completed":
        statuses["dev"] = "pending"
        statuses["test"] = "pending"
        statuses["review"] = "pending"
        return statuses

    if statuses["dev"] == "pending":
        statuses["dev"] = "ready"
        statuses["test"] = "pending"
        statuses["review"] = "pending"
        return statuses

    if statuses["dev"] != "completed":
        statuses["test"] = "pending"
        statuses["review"] = "pending"
        return statuses

    if statuses["test"] == "pending":
        statuses["test"] = "ready"
        statuses["review"] = "pending"
        return statuses

    if statuses["test"] != "completed":
        statuses["review"] = "pending"
        return statuses

    if statuses["review"] == "pending":
        statuses["review"] = "ready"
    return statuses


def _normalize_team_run_payload(payload: dict[str, object]) -> dict[str, object]:
    team_key = _clean_key(payload.get("team_key"), field_name="team_key")
    team_label = _clean_key(payload.get("team_label"), field_name="team_label")
    definition_version = int(payload.get("definition_version") or 1)
    raw_nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else []
    raw_edges = payload.get("edges") if isinstance(payload.get("edges"), list) else []
    nodes: list[dict[str, object]] = []
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            continue
        nodes.append(
            {
                "task_key": _clean_key(raw_node.get("task_key"), field_name="task_key"),
                "label": _clean_key(raw_node.get("label"), field_name="label"),
                "owner_role_key": _clean_key(raw_node.get("owner_role_key"), field_name="owner_role_key"),
                "owner_persona_key": _clean_text(raw_node.get("owner_persona_key")),
                "owner_agent_key": _clean_text(raw_node.get("owner_agent_key")),
                "status": _clean_text(raw_node.get("status")) or "pending",
                "dependency_keys": _clean_string_list(raw_node.get("dependency_keys")),
                "artifact_contract": dict(raw_node.get("artifact_contract") or {}),
                "approval_rule": dict(raw_node.get("approval_rule") or {}),
                "executor_kind": _clean_text(raw_node.get("executor_kind")),
                "summary": _clean_text(raw_node.get("summary")),
                "attempt": int(raw_node.get("attempt") or 0) or None,
            }
        )
    edges: list[dict[str, object]] = []
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            continue
        edges.append(
            {
                "from_task_key": _clean_key(raw_edge.get("from_task_key"), field_name="from_task_key"),
                "to_task_key": _clean_key(raw_edge.get("to_task_key"), field_name="to_task_key"),
            }
        )
    return {
        "team_key": team_key,
        "team_label": team_label,
        "definition_version": definition_version,
        "nodes": nodes,
        "edges": edges,
        "artifacts": list(payload.get("artifacts") or []),
        "approvals": list(payload.get("approvals") or []),
        "status": _clean_text(payload.get("status")) or "queued",
        "runtime_state": dict(payload.get("runtime_state") or {}),
    }


class PlatformTeamRunSnapshotFactory:
    def build_team_run_snapshot(self, *, session: Session, template: PlatformTeamTemplate) -> dict[str, object]:
        roles = platform_catalog_projection_service.role_rows(session=session, template_id=template.template_id)
        tasks = platform_catalog_projection_service.task_rows(session=session, template_id=template.template_id)
        edges = platform_catalog_projection_service.edge_rows(session=session, template_id=template.template_id)
        persona_by_id = {persona.persona_id: persona for persona in platform_catalog_projection_service.list_personas(session=session)}
        agent_by_id = {agent.agent_id: agent for agent in platform_catalog_projection_service.list_agents(session=session)}
        role_binding_by_key: dict[str, dict[str, object]] = {}
        for role in roles:
            persona = persona_by_id.get(role.persona_id)
            agent = agent_by_id.get(role.agent_id)
            role_binding_by_key[role.role_key] = {
                "role_key": role.role_key,
                "label": role.label,
                "persona_key": persona.persona_key if persona is not None else None,
                "agent_key": agent.agent_key if agent is not None else None,
            }
        dependency_map: dict[str, list[str]] = {task.task_key: [] for task in tasks}
        for edge in edges:
            dependency_map.setdefault(edge.to_task_key, []).append(edge.from_task_key)
        nodes: list[dict[str, object]] = []
        for task in tasks:
            binding = role_binding_by_key.get(task.owner_role_key, {})
            nodes.append(
                {
                    "task_key": task.task_key,
                    "label": task.label,
                    "owner_role_key": task.owner_role_key,
                    "owner_persona_key": binding.get("persona_key"),
                    "owner_agent_key": binding.get("agent_key"),
                    "status": "pending",
                    "dependency_keys": list(dependency_map.get(task.task_key, [])),
                    "executor_kind": _clean_text(task.executor_kind),
                    "artifact_contract": dict(task.artifact_contract or {}),
                    "approval_rule": dict(task.approval_rule or {}),
                }
            )
        ready_nodes = {node["task_key"] for node in nodes if not node["dependency_keys"]}
        for node in nodes:
            if node["task_key"] in ready_nodes:
                node["status"] = "ready"
        return {
            "team_key": template.team_key,
            "team_label": template.label,
            "definition_version": int(template.definition_version),
            "nodes": nodes,
            "edges": [
                {"from_task_key": edge.from_task_key, "to_task_key": edge.to_task_key}
                for edge in edges
            ],
            "artifacts": [],
            "approvals": [],
            "status": "queued",
        }

    def build_issue_workflow_team_run_snapshot(
        self,
        *,
        session: Session,
        snapshot: ExecutionSnapshot,
        entry_mode: str | None,
        entry_stage: str | None,
        ensure_issue_workflow_template: Callable[[Session], PlatformTeamTemplate],
        max_loops: int | None = None,
    ) -> dict[str, object]:
        node_statuses = _issue_node_statuses(
            snapshot=snapshot,
            entry_mode=entry_mode,
            entry_stage=entry_stage,
        )
        runtime_state = _issue_runtime_state_from_snapshot(
            snapshot=snapshot,
            entry_mode=entry_mode,
            entry_stage=entry_stage,
            max_loops=max_loops,
        )
        node_status_by_executor = {
            ISSUE_WORKFLOW_STAGE_TO_EXECUTOR_KIND[stage_key]: node_statuses[stage_key]
            for stage_key in ISSUE_WORKFLOW_STAGE_KEYS
        }
        template = ensure_issue_workflow_template(session)
        team_run = self.build_team_run_snapshot(session=session, template=template)
        nodes = [dict(node) for node in team_run.get("nodes", []) if isinstance(node, dict)]
        for node in nodes:
            executor_kind = str(node.get("executor_kind") or "").strip().lower()
            if executor_kind in node_status_by_executor:
                node["status"] = node_status_by_executor[executor_kind]
        team_run["nodes"] = nodes
        team_run["status"] = str(snapshot.workflow.outcome or "").strip() or "queued"
        team_run["runtime_state"] = runtime_state
        return team_run

    def extract_team_run_from_plan(self, *, plan: object | None) -> dict[str, object] | None:
        snapshot = ExecutionSnapshot.load(plan)
        if snapshot is not None:
            raw_team_run = snapshot.context.execution_context.get("team_run")
            if isinstance(raw_team_run, dict):
                return _normalize_team_run_payload(raw_team_run)
        return None


platform_team_run_snapshot_factory = PlatformTeamRunSnapshotFactory()
