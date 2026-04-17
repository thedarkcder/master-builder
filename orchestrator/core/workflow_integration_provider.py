from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from orchestrator.api.discord.seed.issue_service import list_child_issue_previews_for_parent
from orchestrator.api.jira_oauth.connection_service import tenant_jira_oauth_context


@dataclass
class JiraWorkflowAdapter:
    oauth_context: Any
    list_child_issue_previews_for_parent_fn: Callable[..., Any]

    @property
    def client(self):  # noqa: ANN201
        return self.oauth_context.client

    @property
    def access_token(self) -> str:
        return str(self.oauth_context.access_token or "").strip()

    @property
    def cloud_id(self) -> str:
        return str(self.oauth_context.connection.cloud_id or "").strip()

    @property
    def site_url(self) -> str | None:
        value = str(getattr(self.oauth_context.connection, "site_url", "") or "").strip()
        return value or None

    def get_issue_detail(self, *, issue_id_or_key: str):  # noqa: ANN201
        return self.client.get_issue_detail(
            access_token=self.access_token,
            cloud_id=self.cloud_id,
            issue_id_or_key=issue_id_or_key,
        )

    def list_child_issue_previews(self, *, project_key: str, parent_issue_key: str):  # noqa: ANN201
        return self.list_child_issue_previews_for_parent_fn(
            oauth={
                "client": self.client,
                "access_token": self.access_token,
                "cloud_id": self.cloud_id,
            },
            project_key=project_key,
            parent_issue_key=parent_issue_key,
        )


@dataclass
class JiraWorkflowConnectionProvider:
    oauth_context_resolver: Callable[..., Any] = tenant_jira_oauth_context
    list_child_issue_previews_for_parent_fn: Callable[..., Any] = list_child_issue_previews_for_parent

    def adapter_for(self, *, session, tenant, settings) -> JiraWorkflowAdapter:  # noqa: ANN001
        oauth_context = self.oauth_context_resolver(session=session, tenant=tenant, settings=settings)
        return JiraWorkflowAdapter(
            oauth_context=oauth_context,
            list_child_issue_previews_for_parent_fn=self.list_child_issue_previews_for_parent_fn,
        )


@dataclass
class WorkflowIntegrationAdapterProvider:
    jira_provider: JiraWorkflowConnectionProvider = field(default_factory=JiraWorkflowConnectionProvider)

    def jira(self, *, session, tenant, settings) -> JiraWorkflowAdapter:  # noqa: ANN001
        return self.jira_provider.adapter_for(session=session, tenant=tenant, settings=settings)
