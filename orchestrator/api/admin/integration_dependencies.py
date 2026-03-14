from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.admin import route_helpers as admin_route_helpers
from orchestrator.api.admin.discord_allowlist_helpers import (
    notify_discord_allowlist_approved as notify_discord_allowlist_approved_core,
)
from orchestrator.api.admin.jira_webhook_delete import (
    delete_jira_webhooks as delete_jira_webhooks_core,
)
from orchestrator.api.admin.jira_webhook_helpers import (
    extract_jira_webhook_conflict_url,
    is_jira_webhook_limit_error,
    is_jira_webhook_single_url_error,
    jira_webhook_callback_url,
    jira_webhook_filter_jql,
    parse_jira_webhook_id,
    parse_managed_webhook_ids,
)
from orchestrator.api.admin.jira_webhook_provision import (
    provision_jira_webhook as provision_jira_webhook_core,
)
from orchestrator.api.admin.release_bootstrap_helpers import (
    release_bootstrap_report_from_config as release_bootstrap_report_from_config_impl,
)
from orchestrator.api.admin.route_helpers import (
    cleanup_conflicting_jira_webhook_url,
    cleanup_unmanaged_jira_webhooks_for_connection,
    jira_oauth_client as jira_oauth_client_impl,
    refresh_jira_connection_tokens as refresh_jira_connection_tokens_impl,
    remove_managed_webhook_id_from_tenants,
)
from orchestrator.api.schemas import JiraWebhookActionResult, ReleaseBootstrapReportRead
from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.storage.models import JiraOAuthConnection, Project, Tenant
from orchestrator.tools.discord_api import DiscordApiClient
from orchestrator.tools import github_app

JIRA_WEBHOOK_EVENTS = [
    "jira:issue_created",
    "jira:issue_updated",
    "jira:issue_deleted",
    "comment_created",
    "comment_updated",
]
RELEASE_BOOTSTRAP_REQUIRED_STATUSES = ("Ready to Release", "Done")
with_managed_github_refs = admin_route_helpers.with_managed_github_refs
github_client_from_tenant_config = github_app.github_client_from_tenant_config


def notify_discord_allowlist_approved(
    *,
    session: Session,
    settings,
    tenant_id: str,
    user_id: str,
) -> bool:  # noqa: ANN001
    return notify_discord_allowlist_approved_core(
        session=session,
        settings=settings,
        tenant_id=tenant_id,
        user_id=user_id,
        resolve_secret_ref_fn=resolve_platform_secret_ref,
        discord_client_factory=DiscordApiClient,
    )


def jira_oauth_client(
    *,
    session: Session,
    settings,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> object:  # noqa: ANN401
    return jira_oauth_client_impl(
        session=session,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
    )


def refresh_jira_connection_tokens(
    session: Session,
    *,
    connection: JiraOAuthConnection,
    settings,
    tenant_id: str | None = None,
) -> str:
    return refresh_jira_connection_tokens_impl(
        session,
        connection=connection,
        settings=settings,
        tenant_id=tenant_id,
    )


def project_for_tenant_or_404(*, session: Session, tenant_id: str, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


def provision_jira_webhook(
    *,
    session: Session,
    tenant: Tenant,
    settings,  # noqa: ANN001
    replace_existing: bool,
) -> JiraWebhookActionResult:
    return provision_jira_webhook_core(
        session=session,
        tenant=tenant,
        settings=settings,
        replace_existing=replace_existing,
        jira_webhook_events=JIRA_WEBHOOK_EVENTS,
        delete_jira_webhooks_fn=delete_jira_webhooks,
        parse_managed_webhook_ids_fn=parse_managed_webhook_ids,
        refresh_jira_connection_tokens_fn=refresh_jira_connection_tokens,
        jira_oauth_client_fn=jira_oauth_client,
        jira_webhook_callback_url_fn=jira_webhook_callback_url,
        jira_webhook_filter_jql_fn=jira_webhook_filter_jql,
        is_jira_webhook_limit_error_fn=is_jira_webhook_limit_error,
        cleanup_unmanaged_jira_webhooks_for_connection_fn=cleanup_unmanaged_jira_webhooks_for_connection,
        parse_jira_webhook_id_fn=parse_jira_webhook_id,
        remove_managed_webhook_id_from_tenants_fn=remove_managed_webhook_id_from_tenants,
        is_jira_webhook_single_url_error_fn=is_jira_webhook_single_url_error,
        extract_jira_webhook_conflict_url_fn=extract_jira_webhook_conflict_url,
        cleanup_conflicting_jira_webhook_url_fn=cleanup_conflicting_jira_webhook_url,
    )


def delete_jira_webhooks(
    *,
    session: Session,
    tenant: Tenant,
    settings,  # noqa: ANN001
) -> tuple[bool, str, list[int]]:
    return delete_jira_webhooks_core(
        session=session,
        tenant=tenant,
        settings=settings,
        parse_managed_webhook_ids_fn=parse_managed_webhook_ids,
        refresh_jira_connection_tokens_fn=refresh_jira_connection_tokens,
        jira_oauth_client_fn=jira_oauth_client,
    )


def release_bootstrap_report_from_config(
    *,
    tenant_id: str,
    jira_config: dict,
) -> ReleaseBootstrapReportRead | None:
    return release_bootstrap_report_from_config_impl(
        tenant_id=tenant_id,
        jira_config=jira_config,
    )
