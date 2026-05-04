from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from orchestrator.core.platform.secret_manager import (
    list_managed_secret_refs,
    normalize_secret_ref,
    resolve_platform_secret_ref as _resolve_platform_secret_ref,
    resolve_secret_ref_metadata as _resolve_secret_ref_metadata,
    upsert_managed_secret as _upsert_managed_secret,
)

PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF = "DISCORD_BOT_TOKEN"
PLATFORM_SECRET_DISCORD_GUILD_ID_REF = "DISCORD_GUILD_ID"
PLATFORM_SECRET_DISCORD_OAUTH_CLIENT_ID_REF = "DISCORD_OAUTH_CLIENT_ID"
PLATFORM_SECRET_DISCORD_OAUTH_CLIENT_SECRET_REF = "DISCORD_OAUTH_CLIENT_SECRET"
PLATFORM_SECRET_GITHUB_APP_ID_REF = "GITHUB_APP_ID"
PLATFORM_SECRET_GITHUB_APP_SLUG_REF = "GITHUB_APP_SLUG"
PLATFORM_SECRET_GITHUB_PRIVATE_KEY_REF = "GITHUB_APP_PRIVATE_KEY"
PLATFORM_SECRET_ATLASSIAN_OAUTH_CLIENT_ID_REF = "ATLASSIAN_OAUTH_CLIENT_ID"
PLATFORM_SECRET_ATLASSIAN_OAUTH_CLIENT_SECRET_REF = "ATLASSIAN_OAUTH_CLIENT_SECRET"
PLATFORM_SECRET_GITHUB_WEBHOOK_SECRET_REF = "GITHUB_WEBHOOK_SECRET"
PLATFORM_SECRET_ADMIN_PASSWORD_HASH_REF = "ADMIN_PASSWORD_HASH"
_PLATFORM_SCOPE_PREFIX = "platform/"


def _platform_secret_ref(secret_name: str) -> str:
    normalized_secret_name = normalize_secret_ref(secret_name)
    if normalized_secret_name.startswith(_PLATFORM_SCOPE_PREFIX):
        if normalized_secret_name == f"{_PLATFORM_SCOPE_PREFIX}":
            raise ValueError("Platform secret reference is required")
        return normalized_secret_name

    if "/" in normalized_secret_name:
        raise ValueError("Platform secrets must use platform/* refs")

    if normalized_secret_name == "platform":
        raise ValueError("Platform secret reference is required")

    return f"{_PLATFORM_SCOPE_PREFIX}{normalized_secret_name}"


@dataclass(frozen=True)
class PlatformSecretService:
    """Service for platform-scoped secret operations."""

    def get(
        self,
        *,
        session: Session,
        secret_ref: str,
        encryption_key: str,
        allow_environment_fallback: bool = False,
    ) -> str | None:
        platform_secret_ref = _platform_secret_ref(secret_ref)
        return _resolve_platform_secret_ref(
            session,
            secret_ref=platform_secret_ref,
            encryption_key=encryption_key,
            allow_environment_fallback=allow_environment_fallback,
        )

    def list_secret_refs(self, *, session: Session) -> list:
        return list_managed_secret_refs(session, scope="platform")

    def upsert_secret(
        self,
        *,
        session: Session,
        secret_ref: str,
        plaintext_value: str,
        encryption_key: str,
    ):
        platform_secret_ref = _platform_secret_ref(secret_ref)
        return _upsert_managed_secret(
            session,
            secret_ref=platform_secret_ref,
            plaintext_value=plaintext_value,
            encryption_key=encryption_key,
            scope="platform",
        )

    def resolve_secret_metadata(self, *, session: Session, secret_ref: str):
        platform_secret_ref = _platform_secret_ref(secret_ref)
        return _resolve_secret_ref_metadata(session, secret_ref=platform_secret_ref, scope="platform")


platform_secret_service = PlatformSecretService()


def resolve_platform_secret_ref(
    session: Session,
    *,
    secret_ref: str,
    encryption_key: str,
    allow_environment_fallback: bool = False,
) -> str | None:
    return platform_secret_service.get(
        session=session,
        secret_ref=secret_ref,
        encryption_key=encryption_key,
        allow_environment_fallback=allow_environment_fallback,
    )
