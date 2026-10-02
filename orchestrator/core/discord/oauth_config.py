from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from orchestrator.core.config import Settings
from orchestrator.core.platform.secret_service import (
    PLATFORM_SECRET_DISCORD_OAUTH_CLIENT_ID_REF,
    PLATFORM_SECRET_DISCORD_OAUTH_CLIENT_SECRET_REF,
    resolve_platform_secret_ref,
)


@dataclass(frozen=True)
class DiscordOAuthConfig:
    client_id: str
    client_secret: str
    redirect_url: str

    @property
    def authorize_configured(self) -> bool:
        return bool(self.client_id and self.redirect_url)

    @property
    def exchange_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_url)


def resolve_discord_oauth_config(
    *,
    session: Session,
    settings: Settings,
) -> DiscordOAuthConfig:
    managed_client_id = resolve_platform_secret_ref(
        session,
        secret_ref=PLATFORM_SECRET_DISCORD_OAUTH_CLIENT_ID_REF,
        encryption_key=settings.secrets_encryption_key,
    )
    managed_client_secret = resolve_platform_secret_ref(
        session,
        secret_ref=PLATFORM_SECRET_DISCORD_OAUTH_CLIENT_SECRET_REF,
        encryption_key=settings.secrets_encryption_key,
    )
    return DiscordOAuthConfig(
        client_id=str(
            managed_client_id or settings.discord_oauth_client_id or ""
        ).strip(),
        client_secret=str(
            managed_client_secret or settings.discord_oauth_client_secret or ""
        ).strip(),
        redirect_url=str(settings.discord_oauth_redirect_url or "").strip(),
    )
