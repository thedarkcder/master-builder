from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.platform_issue_workflow_catalog import (
    BUILTIN_ISSUE_AGENTS,
    BUILTIN_ISSUE_PERSONAS,
    ISSUE_WORKFLOW_DEV_EXECUTOR_KIND,
    ISSUE_WORKFLOW_PM_EXECUTOR_KIND,
    ISSUE_WORKFLOW_REVIEW_EXECUTOR_KIND,
    ISSUE_WORKFLOW_STAGE_TO_EXECUTOR_KIND,
    ISSUE_WORKFLOW_TEAM_KEY,
    ISSUE_WORKFLOW_TEAM_LABEL,
    ISSUE_WORKFLOW_TEST_EXECUTOR_KIND,
    issue_workflow_template_payload,
)
from orchestrator.core.platform_team_template_compiler import compile_platform_team_template
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot, ExecutionStageRecord
from orchestrator.storage.models import (
    PlatformAgent,
    PlatformPersona,
    PlatformTeamEdge,
    PlatformTeamRole,
    PlatformTeamTask,
    PlatformTeamTemplate,
)

def _now() -> datetime:
    return datetime.now(timezone.utc)


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
        if stage_name in {"dev", "test", "review"} and isinstance(record, ExecutionStageRecord)
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
        "pm": _status_from_stage_record(snapshot.stages.get("pm")),
        "dev": _status_from_stage_record(snapshot.stages.get("dev")),
        "test": _status_from_stage_record(snapshot.stages.get("test")),
        "review": _status_from_stage_record(snapshot.stages.get("review")),
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


@dataclass(frozen=True)
class PlatformTeamCatalogService:
    def list_personas(self, *, session: Session) -> list[PlatformPersona]:
        return session.execute(select(PlatformPersona).order_by(PlatformPersona.persona_key.asc())).scalars().all()

    def get_persona(self, *, session: Session, persona_id: str) -> PlatformPersona | None:
        return session.get(PlatformPersona, str(persona_id or "").strip())

    def get_persona_by_key(self, *, session: Session, persona_key: str) -> PlatformPersona | None:
        return session.execute(
            select(PlatformPersona).where(PlatformPersona.persona_key == _clean_key(persona_key, field_name="persona_key"))
        ).scalar_one_or_none()

    def create_persona(self, *, session: Session, payload: dict) -> PlatformPersona:
        persona_key = _clean_key(payload.get("persona_key"), field_name="persona_key")
        if self.get_persona_by_key(session=session, persona_key=persona_key) is not None:
            raise ValueError(f"Persona already exists: {persona_key}")
        now = _now()
        persona = PlatformPersona(
            persona_id=uuid4().hex,
            persona_key=persona_key,
            label=_clean_key(payload.get("label"), field_name="label"),
            description=_clean_text(payload.get("description")),
            default_display_name=_clean_text(payload.get("default_display_name")),
            default_voice_id=_clean_text(payload.get("default_voice_id")),
            system_prompt_template=_clean_text(payload.get("system_prompt_template")),
            user_prompt_template=_clean_text(payload.get("user_prompt_template")),
            allowed_surfaces=_clean_string_list(payload.get("allowed_surfaces")),
            is_active=bool(payload.get("is_active", True)),
            version=1,
            published_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(persona)
        session.commit()
        session.refresh(persona)
        return persona

    def update_persona(self, *, session: Session, persona_id: str, payload: dict) -> PlatformPersona:
        persona = self.get_persona(session=session, persona_id=persona_id)
        if persona is None:
            raise LookupError("Platform persona not found")
        persona.label = _clean_key(payload.get("label"), field_name="label")
        persona.description = _clean_text(payload.get("description"))
        persona.default_display_name = _clean_text(payload.get("default_display_name"))
        persona.default_voice_id = _clean_text(payload.get("default_voice_id"))
        persona.system_prompt_template = _clean_text(payload.get("system_prompt_template"))
        persona.user_prompt_template = _clean_text(payload.get("user_prompt_template"))
        persona.allowed_surfaces = _clean_string_list(payload.get("allowed_surfaces"))
        persona.is_active = bool(payload.get("is_active", True))
        persona.version += 1
        persona.updated_at = _now()
        session.add(persona)
        session.commit()
        session.refresh(persona)
        return persona

    def list_agents(self, *, session: Session) -> list[PlatformAgent]:
        return session.execute(select(PlatformAgent).order_by(PlatformAgent.agent_key.asc())).scalars().all()

    def get_agent(self, *, session: Session, agent_id: str) -> PlatformAgent | None:
        return session.get(PlatformAgent, str(agent_id or "").strip())

    def get_agent_by_key(self, *, session: Session, agent_key: str) -> PlatformAgent | None:
        return session.execute(
            select(PlatformAgent).where(PlatformAgent.agent_key == _clean_key(agent_key, field_name="agent_key"))
        ).scalar_one_or_none()

    def create_agent(self, *, session: Session, payload: dict) -> PlatformAgent:
        agent_key = _clean_key(payload.get("agent_key"), field_name="agent_key")
        if self.get_agent_by_key(session=session, agent_key=agent_key) is not None:
            raise ValueError(f"Agent already exists: {agent_key}")
        persona = self.get_persona_by_key(session=session, persona_key=_clean_key(payload.get("persona_key"), field_name="persona_key"))
        if persona is None:
            raise ValueError("Referenced persona does not exist")
        now = _now()
        agent = PlatformAgent(
            agent_id=uuid4().hex,
            agent_key=agent_key,
            label=_clean_key(payload.get("label"), field_name="label"),
            description=_clean_text(payload.get("description")),
            persona_id=persona.persona_id,
            runtime_role_key=_clean_text(payload.get("runtime_role_key")),
            named_agent_key=_clean_text(payload.get("named_agent_key")),
            selector_key=_clean_text(payload.get("selector_key")),
            default_profile_name=_clean_text(payload.get("default_profile_name")),
            is_active=bool(payload.get("is_active", True)),
            version=1,
            published_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(agent)
        session.commit()
        session.refresh(agent)
        return agent

    def update_agent(self, *, session: Session, agent_id: str, payload: dict) -> PlatformAgent:
        agent = self.get_agent(session=session, agent_id=agent_id)
        if agent is None:
            raise LookupError("Platform agent not found")
        persona = self.get_persona_by_key(session=session, persona_key=_clean_key(payload.get("persona_key"), field_name="persona_key"))
        if persona is None:
            raise ValueError("Referenced persona does not exist")
        agent.label = _clean_key(payload.get("label"), field_name="label")
        agent.description = _clean_text(payload.get("description"))
        agent.persona_id = persona.persona_id
        agent.runtime_role_key = _clean_text(payload.get("runtime_role_key"))
        agent.named_agent_key = _clean_text(payload.get("named_agent_key"))
        agent.selector_key = _clean_text(payload.get("selector_key"))
        agent.default_profile_name = _clean_text(payload.get("default_profile_name"))
        agent.is_active = bool(payload.get("is_active", True))
        agent.version += 1
        agent.updated_at = _now()
        session.add(agent)
        session.commit()
        session.refresh(agent)
        return agent

    def list_team_templates(self, *, session: Session) -> list[PlatformTeamTemplate]:
        return session.execute(select(PlatformTeamTemplate).order_by(PlatformTeamTemplate.team_key.asc())).scalars().all()

    def list_team_roles(self, *, session: Session, template_id: str) -> list[PlatformTeamRole]:
        return self._role_rows(session=session, template_id=template_id)

    def list_team_tasks(self, *, session: Session, template_id: str) -> list[PlatformTeamTask]:
        return self._task_rows(session=session, template_id=template_id)

    def list_team_edges(self, *, session: Session, template_id: str) -> list[PlatformTeamEdge]:
        return self._edge_rows(session=session, template_id=template_id)

    def get_team_template(self, *, session: Session, template_id: str) -> PlatformTeamTemplate | None:
        return session.get(PlatformTeamTemplate, str(template_id or "").strip())

    def get_team_template_by_key(self, *, session: Session, team_key: str) -> PlatformTeamTemplate | None:
        return session.execute(
            select(PlatformTeamTemplate).where(PlatformTeamTemplate.team_key == _clean_key(team_key, field_name="team_key"))
        ).scalar_one_or_none()

    def create_team_template(self, *, session: Session, payload: dict) -> PlatformTeamTemplate:
        team_key = _clean_key(payload.get("team_key"), field_name="team_key")
        if self.get_team_template_by_key(session=session, team_key=team_key) is not None:
            raise ValueError(f"Team template already exists: {team_key}")
        now = _now()
        template = PlatformTeamTemplate(
            template_id=uuid4().hex,
            team_key=team_key,
            label=_clean_key(payload.get("label"), field_name="label"),
            description=_clean_text(payload.get("description")),
            is_active=bool(payload.get("is_active", True)),
            definition_version=1,
            published_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(template)
        session.flush()
        self._replace_template_children(session=session, template=template, payload=payload)
        session.commit()
        session.refresh(template)
        return template

    def update_team_template(self, *, session: Session, template_id: str, payload: dict) -> PlatformTeamTemplate:
        template = self.get_team_template(session=session, template_id=template_id)
        if template is None:
            raise LookupError("Platform team template not found")
        template.label = _clean_key(payload.get("label"), field_name="label")
        template.description = _clean_text(payload.get("description"))
        template.is_active = bool(payload.get("is_active", True))
        template.updated_at = _now()
        session.add(template)
        self._replace_template_children(session=session, template=template, payload=payload)
        session.commit()
        session.refresh(template)
        return template

    def publish_team_template(self, *, session: Session, template_id: str) -> PlatformTeamTemplate:
        template = self.get_team_template(session=session, template_id=template_id)
        if template is None:
            raise LookupError("Platform team template not found")
        template.definition_version += 1
        template.published_at = _now()
        template.updated_at = template.published_at
        session.add(template)
        session.commit()
        session.refresh(template)
        return template

    def _replace_template_children(self, *, session: Session, template: PlatformTeamTemplate, payload: dict) -> None:
        session.execute(select(PlatformTeamRole).where(PlatformTeamRole.template_id == template.template_id)).scalars().all()
        session.query(PlatformTeamEdge).filter(PlatformTeamEdge.template_id == template.template_id).delete()
        session.query(PlatformTeamTask).filter(PlatformTeamTask.template_id == template.template_id).delete()
        session.query(PlatformTeamRole).filter(PlatformTeamRole.template_id == template.template_id).delete()
        session.flush()

        compiled = compile_platform_team_template(payload)
        now = _now()
        for role in compiled.roles:
            persona = self.get_persona_by_key(session=session, persona_key=role.persona_key)
            agent = self.get_agent_by_key(session=session, agent_key=role.agent_key)
            if persona is None or agent is None:
                raise ValueError(f"Role {role.role_key} references a missing persona or agent")
            session.add(
                PlatformTeamRole(
                    role_id=uuid4().hex,
                    template_id=template.template_id,
                    role_key=role.role_key,
                    label=role.label,
                    description=role.description,
                    position=role.position,
                    persona_id=persona.persona_id,
                    agent_id=agent.agent_id,
                    created_at=now,
                    updated_at=now,
                )
            )

        for task in compiled.tasks:
            session.add(
                PlatformTeamTask(
                    task_id=uuid4().hex,
                    template_id=template.template_id,
                    task_key=task.task_key,
                    label=task.label,
                    owner_role_key=task.owner_role_key,
                    position=task.position,
                    executor_kind=task.executor_kind,
                    artifact_contract=task.artifact_contract,
                    approval_rule=task.approval_rule,
                    created_at=now,
                    updated_at=now,
                )
            )

        for edge in compiled.edges:
            session.add(
                PlatformTeamEdge(
                    edge_id=uuid4().hex,
                    template_id=template.template_id,
                    from_task_key=edge.from_task_key,
                    to_task_key=edge.to_task_key,
                    created_at=now,
                )
            )

    def list_runtime_bindings(self, *, session: Session) -> dict[str, list[str]]:
        roles = sorted(
            {
                str(value or "").strip()
                for value in session.execute(select(PlatformAgent.runtime_role_key)).scalars().all()
                if str(value or "").strip()
            }
        )
        named_agents = sorted(
            {
                str(value or "").strip()
                for value in session.execute(select(PlatformAgent.named_agent_key)).scalars().all()
                if str(value or "").strip()
            }
        )
        selectors = sorted(
            {
                str(value or "").strip()
                for value in session.execute(select(PlatformAgent.selector_key)).scalars().all()
                if str(value or "").strip()
            }
        )
        return {
            "available_roles": roles,
            "available_named_agents": named_agents,
            "available_selectors": selectors,
        }

    def _ensure_builtin_issue_persona(
        self,
        *,
        session: Session,
        persona_key: str,
        label: str,
    ) -> PlatformPersona:
        persona = self.get_persona_by_key(session=session, persona_key=persona_key)
        if persona is not None:
            return persona
        now = _now()
        persona = PlatformPersona(
            persona_id=uuid4().hex,
            persona_key=persona_key,
            label=label,
            description=f"Built-in {label} persona.",
            default_display_name=label,
            default_voice_id=None,
            system_prompt_template=None,
            user_prompt_template=None,
            allowed_surfaces=["team_run_execution"],
            is_active=True,
            version=1,
            published_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(persona)
        session.flush()
        return persona

    def _ensure_builtin_issue_agent(
        self,
        *,
        session: Session,
        agent_key: str,
        label: str,
        persona: PlatformPersona,
        runtime_role_key: str,
        named_agent_key: str,
        selector_key: str,
        default_profile_name: str,
    ) -> PlatformAgent:
        agent = self.get_agent_by_key(session=session, agent_key=agent_key)
        if agent is not None:
            return agent
        now = _now()
        agent = PlatformAgent(
            agent_id=uuid4().hex,
            agent_key=agent_key,
            label=label,
            description=f"Built-in {label} agent.",
            persona_id=persona.persona_id,
            runtime_role_key=runtime_role_key,
            named_agent_key=named_agent_key,
            selector_key=selector_key,
            default_profile_name=default_profile_name,
            is_active=True,
            version=1,
            published_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(agent)
        session.flush()
        return agent

    def ensure_issue_workflow_template(self, *, session: Session) -> PlatformTeamTemplate:
        personas_by_key: dict[str, PlatformPersona] = {}
        for persona_definition in BUILTIN_ISSUE_PERSONAS:
            personas_by_key[persona_definition.persona_key] = self._ensure_builtin_issue_persona(
                session=session,
                persona_key=persona_definition.persona_key,
                label=persona_definition.label,
            )
        for agent_definition in BUILTIN_ISSUE_AGENTS:
            persona = personas_by_key.get(agent_definition.persona_key)
            if persona is None:
                raise ValueError(f"Built-in issue agent references unknown persona: {agent_definition.persona_key}")
            self._ensure_builtin_issue_agent(
                session=session,
                agent_key=agent_definition.agent_key,
                label=agent_definition.label,
                persona=persona,
                runtime_role_key=agent_definition.runtime_role_key,
                named_agent_key=agent_definition.named_agent_key,
                selector_key=agent_definition.selector_key,
                default_profile_name=agent_definition.default_profile_name,
            )
        template = self.get_team_template_by_key(session=session, team_key=ISSUE_WORKFLOW_TEAM_KEY)
        now = _now()
        if template is None:
            template = PlatformTeamTemplate(
                template_id=uuid4().hex,
                team_key=ISSUE_WORKFLOW_TEAM_KEY,
                label=ISSUE_WORKFLOW_TEAM_LABEL,
                description="Built-in issue workflow template.",
                is_active=True,
                definition_version=1,
                published_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(template)
            session.flush()
        template_payload = issue_workflow_template_payload()
        existing_tasks = self._task_rows(session=session, template_id=template.template_id)
        needs_sync = len(existing_tasks) != 4 or any(
            str(task.executor_kind or "").strip().lower()
            not in ISSUE_WORKFLOW_STAGE_TO_EXECUTOR_KIND.values()
            for task in existing_tasks
        )
        if needs_sync:
            self._replace_template_children(session=session, template=template, payload=template_payload)
            template.updated_at = now
            session.add(template)
            session.flush()
        if template.published_at is None:
            template.published_at = now
            template.updated_at = now
            template.definition_version = max(1, int(template.definition_version or 1))
            session.add(template)
            session.flush()
        return template

    def build_team_run_snapshot(self, *, session: Session, template: PlatformTeamTemplate) -> dict[str, object]:
        roles = self._role_rows(session=session, template_id=template.template_id)
        tasks = self._task_rows(session=session, template_id=template.template_id)
        edges = self._edge_rows(session=session, template_id=template.template_id)
        persona_by_id = {persona.persona_id: persona for persona in self.list_personas(session=session)}
        agent_by_id = {agent.agent_id: agent for agent in self.list_agents(session=session)}
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
        session: Session | None = None,
        snapshot: ExecutionSnapshot,
        entry_mode: str | None,
        entry_stage: str | None,
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
            ISSUE_WORKFLOW_STAGE_TO_EXECUTOR_KIND["pm"]: node_statuses["pm"],
            ISSUE_WORKFLOW_STAGE_TO_EXECUTOR_KIND["dev"]: node_statuses["dev"],
            ISSUE_WORKFLOW_STAGE_TO_EXECUTOR_KIND["test"]: node_statuses["test"],
            ISSUE_WORKFLOW_STAGE_TO_EXECUTOR_KIND["review"]: node_statuses["review"],
        }
        if session is None:
            nodes: list[dict[str, object]] = [
                {
                    "task_key": "pm",
                    "label": "PM",
                    "owner_role_key": "pm",
                    "owner_persona_key": "pm",
                    "owner_agent_key": "pm_primary",
                    "status": node_statuses["pm"],
                    "dependency_keys": [],
                    "executor_kind": ISSUE_WORKFLOW_PM_EXECUTOR_KIND,
                    "artifact_contract": {"produces": ["pm_plan"]},
                    "approval_rule": {},
                },
                {
                    "task_key": "dev",
                    "label": "DEV",
                    "owner_role_key": "engineering",
                    "owner_persona_key": "engineering",
                    "owner_agent_key": "workflow_dev_default",
                    "status": node_statuses["dev"],
                    "dependency_keys": ["pm"],
                    "executor_kind": ISSUE_WORKFLOW_DEV_EXECUTOR_KIND,
                    "artifact_contract": {"produces": ["dev_result"]},
                    "approval_rule": {},
                },
                {
                    "task_key": "test",
                    "label": "TEST",
                    "owner_role_key": "test",
                    "owner_persona_key": "test",
                    "owner_agent_key": "workflow_test_default",
                    "status": node_statuses["test"],
                    "dependency_keys": ["dev"],
                    "executor_kind": ISSUE_WORKFLOW_TEST_EXECUTOR_KIND,
                    "artifact_contract": {"produces": ["test_result"]},
                    "approval_rule": {},
                },
                {
                    "task_key": "review",
                    "label": "REVIEW",
                    "owner_role_key": "review",
                    "owner_persona_key": "review",
                    "owner_agent_key": "workflow_review_default",
                    "status": node_statuses["review"],
                    "dependency_keys": ["test"],
                    "executor_kind": ISSUE_WORKFLOW_REVIEW_EXECUTOR_KIND,
                    "artifact_contract": {"produces": ["review_result"]},
                    "approval_rule": {},
                },
            ]
            return {
                "team_key": ISSUE_WORKFLOW_TEAM_KEY,
                "team_label": ISSUE_WORKFLOW_TEAM_LABEL,
                "definition_version": 1,
                "nodes": nodes,
                "edges": [
                    {"from_task_key": "pm", "to_task_key": "dev"},
                    {"from_task_key": "dev", "to_task_key": "test"},
                    {"from_task_key": "test", "to_task_key": "review"},
                ],
                "artifacts": [],
                "approvals": [],
                "status": str(snapshot.workflow.outcome or "").strip() or "queued",
                "runtime_state": runtime_state,
            }
        template = self.ensure_issue_workflow_template(session=session)
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

    def _role_rows(self, *, session: Session, template_id: str) -> list[PlatformTeamRole]:
        return session.execute(
            select(PlatformTeamRole)
            .where(PlatformTeamRole.template_id == template_id)
            .order_by(PlatformTeamRole.position.asc(), PlatformTeamRole.role_key.asc())
        ).scalars().all()

    def _task_rows(self, *, session: Session, template_id: str) -> list[PlatformTeamTask]:
        return session.execute(
            select(PlatformTeamTask)
            .where(PlatformTeamTask.template_id == template_id)
            .order_by(PlatformTeamTask.position.asc(), PlatformTeamTask.task_key.asc())
        ).scalars().all()

    def _edge_rows(self, *, session: Session, template_id: str) -> list[PlatformTeamEdge]:
        return session.execute(
            select(PlatformTeamEdge)
            .where(PlatformTeamEdge.template_id == template_id)
            .order_by(PlatformTeamEdge.from_task_key.asc(), PlatformTeamEdge.to_task_key.asc())
        ).scalars().all()


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

platform_team_catalog_service = PlatformTeamCatalogService()
