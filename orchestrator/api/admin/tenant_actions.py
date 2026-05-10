from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status

from orchestrator.api.schemas import DiscordAllowlistApprovalResult, DiscordAllowlistRequestRead, JiraWebhookActionResult
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.discord_api import DiscordApiError


def disconnect_tenant_atlassian(
    *,
    session,
    tenant: Tenant | None,
    tenant_id: str,
    settings,
    delete_jira_webhooks_fn,
) -> JiraWebhookActionResult:  # noqa: ANN001
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    webhook_delete_ok, delete_details, _ = delete_jira_webhooks_fn(
        session=session,
        tenant=tenant,
        settings=settings,
    )
    if not webhook_delete_ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Atlassian disconnect failed because managed Jira webhook deletion failed: {delete_details}",
        )
    session.refresh(tenant)
    jira_config = dict(tenant.jira_config)
    jira_config["connection_id"] = None
    jira_config["managed_webhook_ids"] = []
    tenant.jira_config = jira_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()

    return JiraWebhookActionResult(
        ok=True,
        action="disconnect",
        details="Atlassian connection disconnected and webhook metadata cleared.",
        webhook_ids=[],
    )


def list_discord_allowlist_requests(
    *,
    session,
    tenant_id: str,
    project_id: str,
    parse_discord_allowlist_requests_fn,
) -> list[DiscordAllowlistRequestRead]:  # noqa: ANN001
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return parse_discord_allowlist_requests_fn(project.discord_config, project_id=project.project_id)


def approve_discord_allowlist_request(
    *,
    session,
    tenant_id: str,
    project_id: str,
    user_id: str,
    settings,
    parse_discord_allowlist_requests_fn,
    notify_discord_allowlist_approved_fn,
) -> DiscordAllowlistApprovalResult:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    normalized_user_id = user_id.strip()
    if not normalized_user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Discord user ID is required")

    discord_config = dict(project.discord_config or {})
    existing_requests = parse_discord_allowlist_requests_fn(discord_config, project_id=project.project_id)
    matching_request = next((item for item in existing_requests if item.user_id == normalized_user_id), None)
    if matching_request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Allowlist request not found")

    allowed_user_ids_raw = discord_config.get("allowed_user_ids")
    allowed_user_ids = (
        [str(value).strip() for value in allowed_user_ids_raw if str(value).strip()]
        if isinstance(allowed_user_ids_raw, list)
        else []
    )
    if normalized_user_id not in allowed_user_ids:
        allowed_user_ids.append(normalized_user_id)
    discord_config["allowed_user_ids"] = allowed_user_ids
    discord_config["allowlist_requests"] = [
        item.model_dump()
        for item in existing_requests
        if item.user_id != normalized_user_id
    ]
    project.discord_config = discord_config
    project.updated_at = datetime.now(timezone.utc)
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()

    notified = False
    notify_error: str | None = None
    try:
        notified = notify_discord_allowlist_approved_fn(
            session=session,
            settings=settings,
            tenant_id=tenant_id,
            user_id=normalized_user_id,
        )
    except (DiscordApiError, ValueError) as exc:
        notify_error = str(exc)

    details = f"Approved allowlist request for {normalized_user_id} on project {project.name}."
    if notified:
        details = f"{details} Sent Discord DM confirmation."
    elif notify_error:
        details = f"{details} DM notification failed: {notify_error}"
    else:
        details = f"{details} DM notification skipped (bot token unavailable)."

    return DiscordAllowlistApprovalResult(
        ok=True,
        details=details,
        project_id=project.project_id,
        user_id=normalized_user_id,
        notified=notified,
    )
