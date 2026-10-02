from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.api.discord.shared.state import resolve_project_for_discord_channel
from orchestrator.core.communications.scope_repository import ResolvedChannelScope
from orchestrator.storage.models import Tenant


class SqlAlchemyDiscordChannelScopeRepository:
    def resolve_project_scope(
        self, *, session: Session, tenant: Tenant, channel_id: str
    ) -> ResolvedChannelScope | None:
        project = resolve_project_for_discord_channel(
            session=session,
            tenant_id=tenant.tenant_id,
            channel_id=channel_id,
        )
        if project is None:
            return None
        return ResolvedChannelScope(
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            jira_project_key=project.jira_project_key,
            channel_id=channel_id,
        )
