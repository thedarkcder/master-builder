from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestrator.core.decision_types import JiraConfigKey, jira_config_text
from orchestrator.storage.models import AtlassianOAuthConnection, Tenant
from orchestrator.tools.atlassian_oauth import AtlassianOAuthError


def delete_jira_webhooks(
    *,
    session: Session,
    tenant: Tenant,
    settings,  # noqa: ANN001
    parse_managed_webhook_ids_fn,
    refresh_atlassian_connection_tokens_fn,
    atlassian_oauth_client_fn,
) -> tuple[bool, str, list[int]]:
    jira_config = dict(tenant.jira_config)
    connection_id = jira_config_text(jira_config=jira_config, key=JiraConfigKey.CONNECTION_ID)
    if not connection_id:
        jira_config["managed_webhook_ids"] = []
        jira_config["webhook_last_error"] = None
        tenant.jira_config = jira_config
        tenant.updated_at = datetime.now(timezone.utc)
        session.commit()
        return True, "No Atlassian connection linked; cleared local webhook metadata.", []

    webhook_ids = parse_managed_webhook_ids_fn(jira_config)
    if not webhook_ids:
        jira_config["webhook_last_error"] = None
        tenant.jira_config = jira_config
        tenant.updated_at = datetime.now(timezone.utc)
        session.commit()
        return True, "No managed Jira webhook IDs stored; nothing to delete.", []

    connection = session.get(AtlassianOAuthConnection, connection_id)
    if connection is None:
        jira_config["managed_webhook_ids"] = []
        jira_config["webhook_last_error"] = "Configured Atlassian connection was not found during webhook deletion"
        tenant.jira_config = jira_config
        tenant.updated_at = datetime.now(timezone.utc)
        session.commit()
        return False, "Configured Atlassian connection was not found.", webhook_ids

    try:
        access_token = refresh_atlassian_connection_tokens_fn(
            session,
            connection=connection,
            settings=settings,
            tenant_id=tenant.tenant_id,
        )
        client = atlassian_oauth_client_fn(session=session, settings=settings, tenant_id=tenant.tenant_id)
        client.delete_webhooks(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            webhook_ids=webhook_ids,
        )
    except (ValueError, AtlassianOAuthError) as exc:
        error_message = f"Failed to delete Jira webhooks: {exc}"
        jira_config["webhook_last_error"] = error_message
        tenant.jira_config = jira_config
        tenant.updated_at = datetime.now(timezone.utc)
        session.commit()
        return False, error_message, webhook_ids

    jira_config["managed_webhook_ids"] = []
    jira_config["webhook_last_error"] = None
    tenant.jira_config = jira_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    return True, f"Deleted {len(webhook_ids)} Jira webhook(s).", webhook_ids
