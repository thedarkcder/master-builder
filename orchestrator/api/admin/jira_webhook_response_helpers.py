from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import status

from orchestrator.api.schemas import JiraWebhookActionResult, JiraWebhookDiagnosticsRead
from orchestrator.core.decision.types import JiraConfigKey, jira_config_text


def jira_webhook_action_status_code(result: JiraWebhookActionResult) -> int:
    if result.ok:
        return status.HTTP_200_OK
    if result.details.startswith("Atlassian connection is not linked"):
        return status.HTTP_400_BAD_REQUEST
    if result.details.startswith("Configured Atlassian connection was not found"):
        return status.HTTP_400_BAD_REQUEST
    return status.HTTP_502_BAD_GATEWAY


def build_jira_webhook_diagnostics(
    *,
    tenant_id: str,
    within_minutes: int,
    jira_config: dict,
    settings,
    jira_webhook_callback_url_fn,
    parse_managed_webhook_ids_fn,
) -> JiraWebhookDiagnosticsRead:  # noqa: ANN001
    connection_id = jira_config_text(
        jira_config=jira_config, key=JiraConfigKey.CONNECTION_ID
    )
    connected = bool(connection_id)
    last_received_at_raw = jira_config.get("webhook_last_received_at")
    last_received_at = (
        last_received_at_raw.strip()
        if isinstance(last_received_at_raw, str) and last_received_at_raw.strip()
        else None
    )
    recent_delivery_ok = False
    if last_received_at:
        try:
            parsed_last_received = datetime.fromisoformat(
                last_received_at.replace("Z", "+00:00")
            )
            threshold = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
            recent_delivery_ok = parsed_last_received >= threshold
        except ValueError:
            recent_delivery_ok = False

    return JiraWebhookDiagnosticsRead(
        tenant_id=tenant_id,
        connected=connected,
        webhook_url=jira_webhook_callback_url_fn(
            settings=settings, tenant_id=tenant_id
        ),
        managed_webhook_ids=parse_managed_webhook_ids_fn(jira_config),
        last_provisioned_at=jira_config.get("webhook_last_provisioned_at"),
        last_received_at=last_received_at,
        last_delivery_id=jira_config.get("webhook_last_delivery_id"),
        last_issue_key=jira_config.get("webhook_last_issue_key"),
        last_error=jira_config.get("webhook_last_error"),
        recent_delivery_window_minutes=within_minutes,
        recent_delivery_ok=recent_delivery_ok,
    )
