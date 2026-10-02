from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from orchestrator.api.discord.bug.service import build_discord_bug_description
from orchestrator.api.discord.shared.response_format import build_jira_issue_url
from orchestrator.storage.models import Tenant
from orchestrator.tools.atlassian_oauth import JiraIssueCreateInput, AtlassianOAuthError

logger = logging.getLogger(__name__)


def create_discord_bug_issue(
    *,
    session: Session,
    tenant: Tenant,
    summary: str,
    details: str,
    reporter_user_id: str,
    channel_id: str | None,
    related_issue_key: str | None,
    attachments: list[dict[str, str]],
    selected_project_key: str | None,
    tenant_project_keys_fn,
    resolve_discord_channel_name_fn,
    tenant_atlassian_oauth_context_fn,
    upload_discord_attachments_to_jira_fn,
    settings,
) -> tuple[str, dict]:
    project_keys = tenant_project_keys_fn(session=session, tenant=tenant)
    if not project_keys:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant has no Jira project keys",
        )
    scoped_project_key = str(selected_project_key or "").strip().upper()
    if not scoped_project_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Bug creation requires a project-scoped Discord channel",
        )
    available_project_keys = {
        str(key).strip().upper() for key in project_keys if str(key).strip()
    }
    if scoped_project_key not in available_project_keys:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Scoped project key {scoped_project_key} is not available for this tenant",
        )
    project_key = scoped_project_key
    channel_display_name = resolve_discord_channel_name_fn(
        session=session,
        tenant=tenant,
        channel_id=channel_id,
    )
    normalized_channel = (
        f"{channel_display_name} ({channel_id})"
        if channel_display_name and channel_id
        else channel_display_name or channel_id
    )

    description = build_discord_bug_description(
        summary=summary,
        details=details,
        reporter_user_id=reporter_user_id,
        channel_id=normalized_channel,
        related_issue_key=related_issue_key,
        attachments=attachments,
    )
    issue_input = JiraIssueCreateInput(
        summary=summary.strip()[:90],
        description=description,
        labels=["discord-bug", "from-discord"],
        issue_type="Bug",
    )

    try:
        oauth = tenant_atlassian_oauth_context_fn(
            session=session, tenant=tenant, settings=settings
        )
        create_result = oauth["client"].create_issues_bulk(
            access_token=oauth["access_token"],
            cloud_id=oauth["connection"].cloud_id,
            project_key=project_key,
            issues=[issue_input],
        )
    except (ValueError, AtlassianOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to create Jira bug: {exc}",
        ) from exc

    if not create_result.created:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Jira bug create returned no issues: {'; '.join(create_result.errors) or 'unknown error'}",
        )

    created_issue = create_result.created[0]
    uploaded_count, upload_failures = upload_discord_attachments_to_jira_fn(
        client=oauth["client"],
        access_token=oauth["access_token"],
        cloud_id=oauth["connection"].cloud_id,
        issue_key=created_issue.key,
        attachments=attachments,
        correlation_id=f"{created_issue.key}:{uuid4().hex}",
    )
    if upload_failures:
        attachment_failures = [failure.warning_message() for failure in upload_failures]
        logger.error(
            "discord_bug_issue_attachment_upload_failed issue_key=%s tenant_id=%s attachment_failures=%s",
            created_issue.key,
            tenant.tenant_id,
            attachment_failures,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bug created as {created_issue.key}, but attachment upload failed: {'; '.join(attachment_failures)}",
        )

    browse_base_url = str(oauth["connection"].site_url or "").strip().rstrip("/")
    issue_url = build_jira_issue_url(
        issue_key=created_issue.key, browse_base_url=browse_base_url
    )
    if issue_url:
        message = f"Bug logged: [{created_issue.key}]({issue_url})"
    else:
        message = f"Bug logged: {created_issue.key}"
    if attachments:
        message = (
            f"{message}. Attached {uploaded_count}/{len(attachments)} file(s) to Jira."
        )
    if create_result.errors:
        message = f"{message} (warnings: {'; '.join(create_result.errors)})"
    return (
        message,
        {
            "created_issue_keys": [created_issue.key],
            "created_issue_links": [issue_url] if issue_url else [],
            "issue_type": "Bug",
            "project_key": project_key,
            "uploaded_attachment_count": uploaded_count,
            "attachment_warnings": [],
        },
    )
