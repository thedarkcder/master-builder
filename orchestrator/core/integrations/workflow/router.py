from __future__ import annotations

from dataclasses import dataclass, field

from orchestrator.core.integrations.workflow.provider import (
    WorkflowIntegrationAdapterProvider,
)


@dataclass
class WorkflowIntegrationRouter:
    adapter_provider: WorkflowIntegrationAdapterProvider = field(
        default_factory=WorkflowIntegrationAdapterProvider
    )

    def jira(self, *, session, tenant, settings):  # noqa: ANN001
        return self.adapter_provider.jira(
            session=session,
            tenant=tenant,
            settings=settings,
        )
