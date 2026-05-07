from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from sqlalchemy.orm import Session

from orchestrator.core.decision.types import JiraConfigKey, tenant_jira_config_text
from orchestrator.storage.models import AtlassianOAuthConnection, Tenant, WorkflowExecution

JIRA_REMOTE_LINK_RELATION_ARCHITECTURE_DOCUMENT = "architecture_document"
JIRA_REMOTE_LINK_RELATION_WORKFLOW_EXECUTION = "workflow_execution"


@dataclass(frozen=True)
class JiraRemoteLinkSpec:
    relation_kind: str
    global_id: str
    relationship: str
    title: str
    url: str


def tenant_jira_browse_base_url(*, session: Session, tenant: Tenant) -> str | None:
    connection_id = tenant_jira_config_text(tenant=tenant, key=JiraConfigKey.CONNECTION_ID)
    if not connection_id:
        return None
    connection = session.get(AtlassianOAuthConnection, connection_id)
    if connection is None:
        return None
    site_url = str(connection.site_url or "").strip().rstrip("/")
    return site_url or None


def tenant_jira_issue_url(*, session: Session, tenant: Tenant, issue_key: str | None) -> str | None:
    normalized_issue_key = str(issue_key or "").strip().upper()
    if not normalized_issue_key:
        return None
    base_url = tenant_jira_browse_base_url(session=session, tenant=tenant)
    if not base_url:
        return None
    return f"{base_url}/browse/{normalized_issue_key}"


def workflow_execution_url(*, admin_ui_base_url: str, tenant_id: str, execution_id: str) -> str:
    normalized_base = str(admin_ui_base_url or "").strip().rstrip("/")
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_execution_id = str(execution_id or "").strip()
    if not normalized_base:
        raise ValueError("admin_ui_base_url is required for workflow execution links")
    if not normalized_tenant_id:
        raise ValueError("tenant_id is required for workflow execution links")
    if not normalized_execution_id:
        raise ValueError("execution_id is required for workflow execution links")
    return (
        f"{normalized_base}/{quote(normalized_tenant_id, safe='')}"
        f"/executions/{quote(normalized_execution_id, safe='')}"
    )


def jira_remote_link_global_id(*, issue_key: str, relation_kind: str) -> str:
    normalized_issue_key = str(issue_key or "").strip().upper()
    normalized_relation_kind = str(relation_kind or "").strip()
    if not normalized_issue_key:
        raise ValueError("issue_key is required for Jira remote link global ids")
    if not normalized_relation_kind:
        raise ValueError("relation_kind is required for Jira remote link global ids")
    return f"system=master-builder&issueKey={normalized_issue_key}&kind={normalized_relation_kind}"


def architecture_document_remote_link_spec(*, issue_key: str, title: str, url: str) -> JiraRemoteLinkSpec:
    normalized_title = str(title or "").strip()
    normalized_url = str(url or "").strip()
    if not normalized_title:
        raise ValueError("title is required for architecture document Jira links")
    if not normalized_url:
        raise ValueError("url is required for architecture document Jira links")
    return JiraRemoteLinkSpec(
        relation_kind=JIRA_REMOTE_LINK_RELATION_ARCHITECTURE_DOCUMENT,
        global_id=jira_remote_link_global_id(
            issue_key=issue_key,
            relation_kind=JIRA_REMOTE_LINK_RELATION_ARCHITECTURE_DOCUMENT,
        ),
        relationship="Architecture",
        title=normalized_title,
        url=normalized_url,
    )


def workflow_execution_remote_link_spec(
    *,
    admin_ui_base_url: str,
    workflow: WorkflowExecution,
) -> JiraRemoteLinkSpec:
    normalized_source_system = str(getattr(workflow, "source_system", "") or "").strip().casefold()
    if normalized_source_system != "jira":
        raise ValueError("workflow execution Jira links require a Jira-sourced workflow")
    normalized_issue_key = str(getattr(workflow, "source_ref", "") or "").strip().upper()
    normalized_execution_id = str(getattr(workflow, "execution_id", "") or "").strip()
    return JiraRemoteLinkSpec(
        relation_kind=JIRA_REMOTE_LINK_RELATION_WORKFLOW_EXECUTION,
        global_id=jira_remote_link_global_id(
            issue_key=normalized_issue_key,
            relation_kind=JIRA_REMOTE_LINK_RELATION_WORKFLOW_EXECUTION,
        ),
        relationship="Execution",
        title="Master Builder Execution",
        url=workflow_execution_url(
            admin_ui_base_url=admin_ui_base_url,
            tenant_id=str(getattr(workflow, "tenant_id", "") or "").strip(),
            execution_id=normalized_execution_id,
        ),
    )
