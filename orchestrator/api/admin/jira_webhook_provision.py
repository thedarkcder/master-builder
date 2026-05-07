from __future__ import annotations

from datetime import datetime, timezone

from orchestrator.api.schemas import JiraWebhookActionResult
from orchestrator.core.decision.types import JiraConfigKey, jira_config_text
from orchestrator.storage.models import AtlassianOAuthConnection, Tenant
from orchestrator.tools.atlassian_oauth import AtlassianOAuthError


def provision_jira_webhook(
    *,
    session,
    tenant: Tenant,
    settings,
    commit: bool = True,
    replace_existing: bool,
    jira_webhook_events: list[str],
    delete_jira_webhooks_fn,
    parse_managed_webhook_ids_fn,
    refresh_atlassian_connection_tokens_fn,
    atlassian_oauth_client_fn,
    jira_webhook_callback_url_fn,
    jira_webhook_filter_jql_fn,
    is_jira_webhook_limit_error_fn,
    cleanup_unmanaged_jira_webhooks_for_connection_fn,
    parse_jira_webhook_id_fn,
    remove_managed_webhook_id_from_tenants_fn,
    is_jira_webhook_single_url_error_fn,
    extract_jira_webhook_conflict_url_fn,
    cleanup_conflicting_jira_webhook_url_fn,
) -> JiraWebhookActionResult:  # noqa: ANN001
    action_name = "reset" if replace_existing else "provision"

    def _persist_tenant_jira_config(updated_jira_config: dict) -> None:
        tenant.jira_config = updated_jira_config
        tenant.updated_at = datetime.now(timezone.utc)
        if commit:
            session.commit()

    jira_config = dict(tenant.jira_config)
    connection_id = jira_config_text(jira_config=jira_config, key=JiraConfigKey.CONNECTION_ID)
    if not connection_id:
        return JiraWebhookActionResult(
            ok=False,
            action=action_name,
            details="Atlassian connection is not linked for this tenant",
            webhook_ids=[],
        )

    connection = session.get(AtlassianOAuthConnection, connection_id)
    if connection is None:
        return JiraWebhookActionResult(
            ok=False,
            action=action_name,
            details="Configured Atlassian connection was not found",
            webhook_ids=[],
        )

    prior_managed_webhook_ids = parse_managed_webhook_ids_fn(jira_config)

    access_token = refresh_atlassian_connection_tokens_fn(
        session,
        connection=connection,
        settings=settings,
        tenant_id=tenant.tenant_id,
    )
    client = atlassian_oauth_client_fn(session=session, settings=settings, tenant_id=tenant.tenant_id)
    callback_url = jira_webhook_callback_url_fn(settings=settings, tenant_id=tenant.tenant_id)
    jql_filter = jira_webhook_filter_jql_fn(jira_config)
    webhook_ids: list[int] | None = None
    cleanup_note: str | None = None
    try:
        webhook_ids = client.register_webhook(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            callback_url=callback_url,
            jql_filter=jql_filter,
            events=jira_webhook_events,
        )
    except (ValueError, AtlassianOAuthError) as exc:
        if is_jira_webhook_limit_error_fn(exc):
            try:
                deleted_count, cleanup_details = cleanup_unmanaged_jira_webhooks_for_connection_fn(
                    session=session,
                    client=client,
                    access_token=access_token,
                    cloud_id=connection.cloud_id,
                )
                cleanup_note = cleanup_details
                if deleted_count == 0:
                    current_tenant_ids = parse_managed_webhook_ids_fn(jira_config)
                    if current_tenant_ids:
                        client.delete_webhooks(
                            access_token=access_token,
                            cloud_id=connection.cloud_id,
                            webhook_ids=current_tenant_ids,
                        )
                        deleted_count = len(current_tenant_ids)
                        cleanup_note = (
                            f"{cleanup_details} Deleted {deleted_count} existing tenant Jira webhook(s)."
                        )
                if deleted_count == 0:
                    all_webhooks = client.list_webhooks(access_token=access_token, cloud_id=connection.cloud_id)
                    rollover_id: int | None = None
                    for item in all_webhooks:
                        parsed = parse_jira_webhook_id_fn(item.get("id"))
                        if parsed is not None:
                            rollover_id = parsed
                            break
                    if rollover_id is not None:
                        client.delete_webhooks(
                            access_token=access_token,
                            cloud_id=connection.cloud_id,
                            webhook_ids=[rollover_id],
                        )
                        touched_tenants = remove_managed_webhook_id_from_tenants_fn(
                            session=session,
                            webhook_id=rollover_id,
                        )
                        deleted_count = 1
                        tenant_note = (
                            f" Removed stale managed reference from {touched_tenants} tenant(s)."
                            if touched_tenants > 0
                            else ""
                        )
                        cleanup_note = (
                            f"{cleanup_details} Deleted 1 rollover Jira webhook ({rollover_id}) to free capacity."
                            f"{tenant_note}"
                        )
                if deleted_count > 0:
                    webhook_ids = client.register_webhook(
                        access_token=access_token,
                        cloud_id=connection.cloud_id,
                        callback_url=callback_url,
                        jql_filter=jql_filter,
                        events=jira_webhook_events,
                    )
            except (ValueError, AtlassianOAuthError) as cleanup_exc:
                jira_config["webhook_last_error"] = (
                    "Failed to provision Jira webhook: "
                    f"{exc}. Cleanup attempt failed: {cleanup_exc}"
                )
                _persist_tenant_jira_config(jira_config)
                return JiraWebhookActionResult(
                    ok=False,
                    action=action_name,
                    details=jira_config["webhook_last_error"],
                    webhook_ids=parse_managed_webhook_ids_fn(jira_config),
                )
        if webhook_ids is None:
            if is_jira_webhook_single_url_error_fn(exc):
                try:
                    conflicting_url = extract_jira_webhook_conflict_url_fn(exc)
                    deleted_count, cleanup_details = cleanup_conflicting_jira_webhook_url_fn(
                        session=session,
                        client=client,
                        access_token=access_token,
                        cloud_id=connection.cloud_id,
                        callback_url=callback_url,
                        conflicting_url=conflicting_url,
                    )
                    if deleted_count > 0:
                        cleanup_note = cleanup_details
                        webhook_ids = client.register_webhook(
                            access_token=access_token,
                            cloud_id=connection.cloud_id,
                            callback_url=callback_url,
                            jql_filter=jql_filter,
                            events=jira_webhook_events,
                        )
                except (ValueError, AtlassianOAuthError) as cleanup_exc:
                    jira_config["webhook_last_error"] = (
                        "Failed to provision Jira webhook: "
                        f"{exc}. URL-conflict cleanup failed: {cleanup_exc}"
                    )
                    _persist_tenant_jira_config(jira_config)
                    return JiraWebhookActionResult(
                        ok=False,
                        action=action_name,
                        details=jira_config["webhook_last_error"],
                        webhook_ids=parse_managed_webhook_ids_fn(jira_config),
                    )
        if webhook_ids is None:
            jira_config["webhook_last_error"] = f"Failed to provision Jira webhook: {exc}"
            _persist_tenant_jira_config(jira_config)
            return JiraWebhookActionResult(
                ok=False,
                action=action_name,
                details=jira_config["webhook_last_error"],
                webhook_ids=parse_managed_webhook_ids_fn(jira_config),
            )

    now_iso = datetime.now(timezone.utc).isoformat()
    jira_config["managed_webhook_ids"] = webhook_ids
    jira_config["webhook_last_provisioned_at"] = now_iso
    jira_config["webhook_last_error"] = None
    if replace_existing:
        stale_webhook_ids = [webhook_id for webhook_id in prior_managed_webhook_ids if webhook_id not in webhook_ids]
        if stale_webhook_ids:
            try:
                client.delete_webhooks(
                    access_token=access_token,
                    cloud_id=connection.cloud_id,
                    webhook_ids=stale_webhook_ids,
                )
                stale_cleanup_note = f"Deleted {len(stale_webhook_ids)} previous managed Jira webhook(s)."
                cleanup_note = f"{cleanup_note} {stale_cleanup_note}".strip() if cleanup_note else stale_cleanup_note
            except (ValueError, AtlassianOAuthError) as exc:
                stale_cleanup_note = (
                    f"Registered new webhook(s) but could not delete {len(stale_webhook_ids)} previous managed "
                    f"webhook(s): {exc}"
                )
                cleanup_note = f"{cleanup_note} {stale_cleanup_note}".strip() if cleanup_note else stale_cleanup_note
    _persist_tenant_jira_config(jira_config)
    details = f"Provisioned {len(webhook_ids)} Jira webhook(s)."
    if cleanup_note:
        details = f"{details} {cleanup_note}"
    return JiraWebhookActionResult(
        ok=True,
        action=action_name,
        details=details,
        webhook_ids=webhook_ids,
    )
