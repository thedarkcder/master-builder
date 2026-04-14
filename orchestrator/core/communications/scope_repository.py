from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from orchestrator.storage.models import Tenant


@dataclass(frozen=True)
class ResolvedChannelScope:
    tenant_id: str
    project_id: str
    jira_project_key: str
    channel_id: str


class ChannelScopeRepository(Protocol):
    def resolve_project_scope(self, *, session: Session, tenant: Tenant, channel_id: str) -> ResolvedChannelScope | None:
        ...
