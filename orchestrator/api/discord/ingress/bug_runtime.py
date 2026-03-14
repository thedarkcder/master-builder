from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.api.discord.ask.context import tenant_project_keys
from orchestrator.api.discord.bug.attachments import (
    AttachmentUploadFailure,
    download_discord_attachment as _download_discord_attachment_impl,
    resolve_discord_channel_name as _resolve_discord_channel_name_impl,
    upload_discord_attachments_to_jira as _upload_discord_attachments_to_jira_impl,
)
from orchestrator.api.discord.bug.issue_create_service import create_discord_bug_issue as _create_discord_bug_issue_impl
from orchestrator.api.discord.bug.service import (
    build_discord_bug_description as _build_discord_bug_description_impl,
    normalize_discord_attachments as _normalize_discord_attachments_impl,
)
from orchestrator.api.discord.ingress import jira_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    resolve_platform_secret_ref,
)


def resolve_discord_channel_name(*, session: Session, tenant, channel_id: str | None) -> str | None:  # noqa: ANN001
    settings = get_settings()
    return _resolve_discord_channel_name_impl(
        session=session,
        tenant=tenant,
        channel_id=channel_id,
        discord_bot_token_secret_ref=PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
        secrets_encryption_key=settings.secrets_encryption_key,
    )



def download_discord_attachment(*, url: str, bot_token: str | None = None) -> tuple[bytes, str | None]:
    return _download_discord_attachment_impl(url=url, bot_token=bot_token)



def normalize_discord_attachments(raw_attachments: object) -> list[dict[str, str]]:
    return _normalize_discord_attachments_impl(raw_attachments)



def build_discord_bug_description(
    *,
    summary: str,
    details: str,
    reporter_user_id: str,
    channel_id: str | None,
    related_issue_key: str | None,
    attachments: list[dict[str, str]],
) -> str:
    return _build_discord_bug_description_impl(
        summary=summary,
        details=details,
        reporter_user_id=reporter_user_id,
        channel_id=channel_id,
        related_issue_key=related_issue_key,
        attachments=attachments,
    )



def create_discord_bug_issue(
    *,
    session: Session,
    tenant,
    summary: str,
    details: str,
    reporter_user_id: str,
    channel_id: str | None,
    related_issue_key: str | None,
    attachments: list[dict[str, str]],
    selected_project_key: str | None = None,
) -> tuple[str, dict]:  # noqa: ANN001
    settings = get_settings()
    bot_token = resolve_platform_secret_ref(
        session,
        secret_ref=PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
        encryption_key=settings.secrets_encryption_key,
    )

    def _download_attachment(*, url: str) -> tuple[bytes, str | None]:
        return download_discord_attachment(url=url, bot_token=bot_token)

    def _upload_attachments(
        *,
        client,
        access_token: str,
        cloud_id: str,
        issue_key: str,
        attachments: list[dict[str, str]],
        correlation_id: str | None = None,
    ) -> tuple[int, list[AttachmentUploadFailure]]:
        return _upload_discord_attachments_to_jira_impl(
            client=client,
            access_token=access_token,
            cloud_id=cloud_id,
            issue_key=issue_key,
            attachments=attachments,
            correlation_id=correlation_id,
            download_attachment=_download_attachment,
        )

    return _create_discord_bug_issue_impl(
        session=session,
        tenant=tenant,
        summary=summary,
        details=details,
        reporter_user_id=reporter_user_id,
        channel_id=channel_id,
        related_issue_key=related_issue_key,
        attachments=attachments,
        selected_project_key=selected_project_key,
        tenant_project_keys_fn=tenant_project_keys,
        resolve_discord_channel_name_fn=resolve_discord_channel_name,
        tenant_jira_oauth_context_fn=jira_runtime.tenant_jira_oauth_context,
        upload_discord_attachments_to_jira_fn=_upload_attachments,
        settings=settings,
    )
