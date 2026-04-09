from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.platform_catalog_projection_service import platform_catalog_projection_service
from orchestrator.core.platform_issue_workflow_catalog import (
    BUILTIN_ISSUE_AGENTS,
    BUILTIN_ISSUE_PERSONAS,
    ISSUE_WORKFLOW_STAGE_TO_EXECUTOR_KIND,
    ISSUE_WORKFLOW_TEAM_KEY,
    ISSUE_WORKFLOW_TEAM_LABEL,
    issue_workflow_template_payload,
)
from orchestrator.core.platform_team_run_snapshot_factory import platform_team_run_snapshot_factory
from orchestrator.core.platform_team_template_compiler import compile_platform_team_template
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
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


@dataclass(frozen=True)
class PlatformTeamCatalogService:
    def list_personas(self, *, session: Session) -> list[PlatformPersona]:
        return platform_catalog_projection_service.list_personas(session=session)

    def get_persona(self, *, session: Session, persona_id: str) -> PlatformPersona | None:
        return platform_catalog_projection_service.get_persona(session=session, persona_id=persona_id)

    def get_persona_by_key(self, *, session: Session, persona_key: str) -> PlatformPersona | None:
        return platform_catalog_projection_service.get_persona_by_key(session=session, persona_key=persona_key)

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
        return platform_catalog_projection_service.list_agents(session=session)

    def get_agent(self, *, session: Session, agent_id: str) -> PlatformAgent | None:
        return platform_catalog_projection_service.get_agent(session=session, agent_id=agent_id)

    def get_agent_by_key(self, *, session: Session, agent_key: str) -> PlatformAgent | None:
        return platform_catalog_projection_service.get_agent_by_key(session=session, agent_key=agent_key)

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
        return platform_catalog_projection_service.list_team_templates(session=session)

    def list_team_roles(self, *, session: Session, template_id: str) -> list[PlatformTeamRole]:
        return platform_catalog_projection_service.list_team_roles(session=session, template_id=template_id)

    def list_team_tasks(self, *, session: Session, template_id: str) -> list[PlatformTeamTask]:
        return platform_catalog_projection_service.list_team_tasks(session=session, template_id=template_id)

    def list_team_edges(self, *, session: Session, template_id: str) -> list[PlatformTeamEdge]:
        return platform_catalog_projection_service.list_team_edges(session=session, template_id=template_id)

    def get_team_template(self, *, session: Session, template_id: str) -> PlatformTeamTemplate | None:
        return platform_catalog_projection_service.get_team_template(session=session, template_id=template_id)

    def get_team_template_by_key(self, *, session: Session, team_key: str) -> PlatformTeamTemplate | None:
        return platform_catalog_projection_service.get_team_template_by_key(session=session, team_key=team_key)

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
        return platform_catalog_projection_service.list_runtime_bindings(session=session)

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
        existing_tasks = platform_catalog_projection_service.task_rows(session=session, template_id=template.template_id)
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
        return platform_team_run_snapshot_factory.build_team_run_snapshot(session=session, template=template)

    def build_issue_workflow_team_run_snapshot(
        self,
        *,
        session: Session,
        snapshot: ExecutionSnapshot,
        entry_mode: str | None,
        entry_stage: str | None,
        max_loops: int | None = None,
    ) -> dict[str, object]:
        return platform_team_run_snapshot_factory.build_issue_workflow_team_run_snapshot(
            session=session,
            snapshot=snapshot,
            entry_mode=entry_mode,
            entry_stage=entry_stage,
            max_loops=max_loops,
            ensure_issue_workflow_template=lambda active_session: self.ensure_issue_workflow_template(session=active_session),
        )

    def extract_team_run_from_plan(self, *, plan: object | None) -> dict[str, object] | None:
        return platform_team_run_snapshot_factory.extract_team_run_from_plan(plan=plan)


platform_team_catalog_service = PlatformTeamCatalogService()
