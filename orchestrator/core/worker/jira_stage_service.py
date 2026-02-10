from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from orchestrator.api.jira_oauth_service import jira_oauth_client, refresh_jira_connection_tokens
from orchestrator.storage.models import JiraOAuthConnection, Tenant
from orchestrator.tools.jira_oauth import JiraOAuthError

logger = logging.getLogger(__name__)

JIRA_STAGE_COMMENT_EVENTS = {"decision_gate_required", "run_failed"}


def send_stage_update_to_jira(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str | None,
    stage: str,
    message: str,
    settings,
) -> None:  # noqa: ANN001
    if not issue_key or not message.strip():
        return
    if stage not in JIRA_STAGE_COMMENT_EVENTS:
        return

    jira_config = tenant.jira_config or {}
    connection_id = str(jira_config.get("connection_id") or "").strip()
    if not connection_id:
        logger.info(
            "worker_jira_stage_update_not_sent tenant_id=%s issue_key=%s stage=%s reason=missing_connection",
            tenant.tenant_id,
            issue_key,
            stage,
        )
        return

    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        logger.info(
            "worker_jira_stage_update_not_sent tenant_id=%s issue_key=%s stage=%s reason=connection_not_found",
            tenant.tenant_id,
            issue_key,
            stage,
        )
        return

    try:
        access_token = refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
            tenant_id=tenant.tenant_id,
        )
        client = jira_oauth_client(session=session, settings=settings, tenant_id=tenant.tenant_id)
        client.add_issue_comment(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            issue_id_or_key=issue_key,
            comment=message,
        )
    except (JiraOAuthError, ValueError) as exc:
        logger.warning(
            "worker_jira_stage_update_send_failed tenant_id=%s issue_key=%s stage=%s error=%s",
            tenant.tenant_id,
            issue_key,
            stage,
            exc,
        )
