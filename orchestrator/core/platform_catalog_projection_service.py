from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.storage.models import (
    PlatformAgent,
    PlatformPersona,
    PlatformTeamEdge,
    PlatformTeamRole,
    PlatformTeamTask,
    PlatformTeamTemplate,
)


class PlatformCatalogProjectionService:
    def list_personas(self, *, session: Session) -> list[PlatformPersona]:
        return session.execute(select(PlatformPersona).order_by(PlatformPersona.persona_key.asc())).scalars().all()

    def get_persona(self, *, session: Session, persona_id: str) -> PlatformPersona | None:
        return session.get(PlatformPersona, str(persona_id or "").strip())

    def get_persona_by_key(self, *, session: Session, persona_key: str) -> PlatformPersona | None:
        normalized_key = str(persona_key or "").strip()
        if not normalized_key:
            return None
        return session.execute(select(PlatformPersona).where(PlatformPersona.persona_key == normalized_key)).scalar_one_or_none()

    def list_agents(self, *, session: Session) -> list[PlatformAgent]:
        return session.execute(select(PlatformAgent).order_by(PlatformAgent.agent_key.asc())).scalars().all()

    def get_agent(self, *, session: Session, agent_id: str) -> PlatformAgent | None:
        return session.get(PlatformAgent, str(agent_id or "").strip())

    def get_agent_by_key(self, *, session: Session, agent_key: str) -> PlatformAgent | None:
        normalized_key = str(agent_key or "").strip()
        if not normalized_key:
            return None
        return session.execute(select(PlatformAgent).where(PlatformAgent.agent_key == normalized_key)).scalar_one_or_none()

    def list_team_templates(self, *, session: Session) -> list[PlatformTeamTemplate]:
        return session.execute(select(PlatformTeamTemplate).order_by(PlatformTeamTemplate.team_key.asc())).scalars().all()

    def get_team_template(self, *, session: Session, template_id: str) -> PlatformTeamTemplate | None:
        return session.get(PlatformTeamTemplate, str(template_id or "").strip())

    def get_team_template_by_key(self, *, session: Session, team_key: str) -> PlatformTeamTemplate | None:
        normalized_key = str(team_key or "").strip()
        if not normalized_key:
            return None
        return session.execute(
            select(PlatformTeamTemplate).where(PlatformTeamTemplate.team_key == normalized_key)
        ).scalar_one_or_none()

    def list_team_roles(self, *, session: Session, template_id: str) -> list[PlatformTeamRole]:
        return self.role_rows(session=session, template_id=template_id)

    def list_team_tasks(self, *, session: Session, template_id: str) -> list[PlatformTeamTask]:
        return self.task_rows(session=session, template_id=template_id)

    def list_team_edges(self, *, session: Session, template_id: str) -> list[PlatformTeamEdge]:
        return self.edge_rows(session=session, template_id=template_id)

    def role_rows(self, *, session: Session, template_id: str) -> list[PlatformTeamRole]:
        return session.execute(
            select(PlatformTeamRole)
            .where(PlatformTeamRole.template_id == template_id)
            .order_by(PlatformTeamRole.position.asc(), PlatformTeamRole.role_key.asc())
        ).scalars().all()

    def task_rows(self, *, session: Session, template_id: str) -> list[PlatformTeamTask]:
        return session.execute(
            select(PlatformTeamTask)
            .where(PlatformTeamTask.template_id == template_id)
            .order_by(PlatformTeamTask.position.asc(), PlatformTeamTask.task_key.asc())
        ).scalars().all()

    def edge_rows(self, *, session: Session, template_id: str) -> list[PlatformTeamEdge]:
        return session.execute(
            select(PlatformTeamEdge)
            .where(PlatformTeamEdge.template_id == template_id)
            .order_by(PlatformTeamEdge.from_task_key.asc(), PlatformTeamEdge.to_task_key.asc())
        ).scalars().all()

    def list_runtime_bindings(self, *, session: Session) -> dict[str, list[str]]:
        roles = sorted(
            {
                str(value or "").strip()
                for value in session.execute(
                    select(PlatformAgent.runtime_role_key).where(PlatformAgent.is_active.is_(True))
                ).scalars().all()
                if str(value or "").strip()
            }
        )
        named_agents = sorted(
            {
                str(value or "").strip()
                for value in session.execute(
                    select(PlatformAgent.named_agent_key).where(PlatformAgent.is_active.is_(True))
                ).scalars().all()
                if str(value or "").strip()
            }
        )
        selectors = sorted(
            {
                str(value or "").strip()
                for value in session.execute(
                    select(PlatformAgent.selector_key).where(PlatformAgent.is_active.is_(True))
                ).scalars().all()
                if str(value or "").strip()
            }
        )
        return {
            "available_roles": roles,
            "available_named_agents": named_agents,
            "available_selectors": selectors,
        }


platform_catalog_projection_service = PlatformCatalogProjectionService()
