from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from orchestrator.api.atlassian_oauth.service import (
    atlassian_oauth_client,
    refresh_atlassian_connection_tokens,
)
from orchestrator.core.decision.types import (
    JiraConfigKey,
    WorkerStageEvent,
    tenant_jira_config_text,
)
from orchestrator.storage.models import AtlassianOAuthConnection, Tenant
from orchestrator.tools.atlassian_oauth import AtlassianOAuthError

logger = logging.getLogger(__name__)

JIRA_STAGE_COMMENT_EVENTS = {
    WorkerStageEvent.DECISION_GATE_REQUIRED.value,
    "run_not_ready",
    "run_failed",
    "test_feedback",
    "dev_rationale",
    "review_summary",
    "review_feedback",
}


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
    normalized_stage = str(stage or "").strip()
    if not normalized_stage or normalized_stage not in JIRA_STAGE_COMMENT_EVENTS:
        return

    connection_id = tenant_jira_config_text(
        tenant=tenant, key=JiraConfigKey.CONNECTION_ID
    )
    if not connection_id:
        logger.info(
            "worker_jira_stage_update_not_sent tenant_id=%s issue_key=%s stage=%s reason=missing_connection",
            tenant.tenant_id,
            issue_key,
            normalized_stage,
        )
        return

    connection = session.get(AtlassianOAuthConnection, connection_id)
    if connection is None:
        logger.info(
            "worker_jira_stage_update_not_sent tenant_id=%s issue_key=%s stage=%s reason=connection_not_found",
            tenant.tenant_id,
            issue_key,
            normalized_stage,
        )
        return

    try:
        access_token = refresh_atlassian_connection_tokens(
            session,
            connection=connection,
            settings=settings,
            tenant_id=tenant.tenant_id,
        )
        client = atlassian_oauth_client(
            session=session, settings=settings, tenant_id=tenant.tenant_id
        )
        client.add_issue_comment(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            issue_id_or_key=issue_key,
            comment=message,
        )
    except (AtlassianOAuthError, ValueError) as exc:
        logger.warning(
            "worker_jira_stage_update_send_failed tenant_id=%s issue_key=%s stage=%s error=%s",
            tenant.tenant_id,
            issue_key,
            normalized_stage,
            exc,
        )


def transition_issue_status(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str | None,
    target_status: str,
    settings,
) -> None:  # noqa: ANN001
    normalized_issue_key = str(issue_key or "").strip()
    normalized_target_status = str(target_status or "").strip()
    if not normalized_issue_key or not normalized_target_status:
        return

    connection_id = tenant_jira_config_text(
        tenant=tenant, key=JiraConfigKey.CONNECTION_ID
    )
    if not connection_id:
        logger.info(
            "worker_jira_transition_not_sent tenant_id=%s issue_key=%s target_status=%s reason=missing_connection",
            tenant.tenant_id,
            normalized_issue_key,
            normalized_target_status,
        )
        return

    connection = session.get(AtlassianOAuthConnection, connection_id)
    if connection is None:
        logger.info(
            "worker_jira_transition_not_sent tenant_id=%s issue_key=%s target_status=%s reason=connection_not_found",
            tenant.tenant_id,
            normalized_issue_key,
            normalized_target_status,
        )
        return

    try:
        access_token = refresh_atlassian_connection_tokens(
            session,
            connection=connection,
            settings=settings,
            tenant_id=tenant.tenant_id,
        )
        client = atlassian_oauth_client(
            session=session, settings=settings, tenant_id=tenant.tenant_id
        )
        client.transition_issue(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            issue_id_or_key=normalized_issue_key,
            target_status=normalized_target_status,
        )
    except (AtlassianOAuthError, ValueError) as exc:
        logger.warning(
            "worker_jira_transition_failed tenant_id=%s issue_key=%s target_status=%s error=%s",
            tenant.tenant_id,
            normalized_issue_key,
            normalized_target_status,
            exc,
        )
