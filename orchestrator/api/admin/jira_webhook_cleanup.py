from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.storage.models import Tenant
from orchestrator.tools.atlassian_oauth import AtlassianOAuthClient


def all_managed_webhook_ids(
    *,
    session: Session,
    parse_managed_webhook_ids_fn,
) -> set[int]:
    managed: set[int] = set()
    tenants = session.execute(select(Tenant.jira_config)).all()
    for (jira_config_raw,) in tenants:
        if not isinstance(jira_config_raw, dict):
            continue
        managed.update(parse_managed_webhook_ids_fn(jira_config_raw))
    return managed


def remove_managed_webhook_id_from_tenants(
    *,
    session: Session,
    webhook_id: int,
    parse_managed_webhook_ids_fn,
) -> int:
    removed_count = 0
    tenants = session.execute(select(Tenant)).scalars().all()
    for tenant in tenants:
        jira_config = tenant.jira_config
        if not isinstance(jira_config, dict):
            continue
        managed_ids = parse_managed_webhook_ids_fn(jira_config)
        if webhook_id not in managed_ids:
            continue
        updated_ids = [item for item in managed_ids if item != webhook_id]
        updated_config = dict(jira_config)
        updated_config["managed_webhook_ids"] = updated_ids
        tenant.jira_config = updated_config
        tenant.updated_at = datetime.now(timezone.utc)
        removed_count += 1
    return removed_count


def cleanup_unmanaged_jira_webhooks_for_connection(
    *,
    session: Session,
    client: AtlassianOAuthClient,
    access_token: str,
    cloud_id: str,
    parse_managed_webhook_ids_fn,
    parse_jira_webhook_id_fn,
) -> tuple[int, str]:
    webhooks = client.list_webhooks(access_token=access_token, cloud_id=cloud_id)
    if not webhooks:
        return 0, "No existing Jira webhooks were listed for this app/user."

    managed_ids = all_managed_webhook_ids(
        session=session,
        parse_managed_webhook_ids_fn=parse_managed_webhook_ids_fn,
    )
    stale_ids: list[int] = []
    for item in webhooks:
        webhook_id = parse_jira_webhook_id_fn(item.get("id"))
        if webhook_id is None:
            continue
        if webhook_id not in managed_ids:
            stale_ids.append(webhook_id)

    if not stale_ids:
        return 0, "No unmanaged Jira webhooks were found to clean up."

    client.delete_webhooks(
        access_token=access_token,
        cloud_id=cloud_id,
        webhook_ids=stale_ids,
    )
    return len(stale_ids), f"Deleted {len(stale_ids)} unmanaged Jira webhook(s)."


def cleanup_conflicting_jira_webhook_url(
    *,
    session: Session,
    client: AtlassianOAuthClient,
    access_token: str,
    cloud_id: str,
    callback_url: str,
    conflicting_url: str | None,
    parse_jira_webhook_id_fn,
    remove_managed_webhook_id_from_tenants_fn,
) -> tuple[int, str]:
    target_urls = {
        callback_url.strip().lower().rstrip("/"),
    }
    if conflicting_url:
        target_urls.add(conflicting_url.strip().lower().rstrip("/"))

    webhooks = client.list_webhooks(access_token=access_token, cloud_id=cloud_id)
    delete_ids: list[int] = []
    for item in webhooks:
        webhook_id = parse_jira_webhook_id_fn(item.get("id"))
        webhook_url = str(item.get("url") or "").strip().lower().rstrip("/")
        if webhook_id is None or not webhook_url:
            continue
        if webhook_url in target_urls:
            delete_ids.append(webhook_id)

    if not delete_ids:
        return 0, "No conflicting Jira webhook URL was found to delete."

    client.delete_webhooks(
        access_token=access_token,
        cloud_id=cloud_id,
        webhook_ids=delete_ids,
    )
    touched_tenants = 0
    for webhook_id in delete_ids:
        touched_tenants += remove_managed_webhook_id_from_tenants_fn(
            session=session,
            webhook_id=webhook_id,
        )
    tenant_note = (
        f" Removed stale managed reference from {touched_tenants} tenant(s)."
        if touched_tenants > 0
        else ""
    )
    return (
        len(delete_ids),
        f"Deleted {len(delete_ids)} conflicting Jira webhook URL subscription(s).{tenant_note}",
    )
