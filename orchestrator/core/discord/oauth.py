from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from orchestrator.core.config import Settings


class DiscordOAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscordOAuthState:
    tenant_id: str
    user_id: str
    redirect_to: str
    expires_at: datetime


@dataclass(frozen=True)
class DiscordOAuthUser:
    discord_user_id: str
    username: str | None
    global_name: str | None
    avatar_hash: str | None
    access_token: str


def discord_oauth_is_configured(*, settings: Settings) -> bool:
    return bool(settings.discord_oauth_client_id.strip() and settings.discord_oauth_redirect_url.strip())


def build_discord_oauth_authorize_url(
    *,
    settings: Settings,
    state: str,
) -> str:
    client_id = settings.discord_oauth_client_id.strip()
    redirect_uri = settings.discord_oauth_redirect_url.strip()
    if not discord_oauth_is_configured(settings=settings):
        raise DiscordOAuthError("Discord OAuth is not configured")
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": "identify guilds.join",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"https://discord.com/oauth2/authorize?{query}"


def issue_discord_oauth_state(
    *,
    settings: Settings,
    tenant_id: str,
    user_id: str,
    redirect_to: str,
    ttl_minutes: int = 15,
) -> str:
    expires_at = datetime.now(UTC) + timedelta(minutes=ttl_minutes)
    payload = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "redirect_to": redirect_to,
        "expires_at": expires_at.isoformat(),
    }
    encoded = _urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _sign_state(settings=settings, encoded_payload=encoded)
    return f"{encoded}.{signature}"


def parse_discord_oauth_state(*, settings: Settings, state: str) -> DiscordOAuthState:
    encoded, separator, signature = state.partition(".")
    if not separator or not encoded or not signature:
        raise DiscordOAuthError("Invalid Discord OAuth state")
    expected = _sign_state(settings=settings, encoded_payload=encoded)
    if not hmac.compare_digest(signature, expected):
        raise DiscordOAuthError("Invalid Discord OAuth state")
    try:
        payload = json.loads(_urlsafe_b64decode(encoded).decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise DiscordOAuthError("Invalid Discord OAuth state") from exc
    expires_at_raw = str(payload.get("expires_at") or "").strip()
    try:
        expires_at = datetime.fromisoformat(expires_at_raw)
    except ValueError as exc:
        raise DiscordOAuthError("Invalid Discord OAuth state") from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        raise DiscordOAuthError("Discord OAuth state expired")
    return DiscordOAuthState(
        tenant_id=str(payload.get("tenant_id") or "").strip(),
        user_id=str(payload.get("user_id") or "").strip(),
        redirect_to=str(payload.get("redirect_to") or "/get-started").strip() or "/get-started",
        expires_at=expires_at,
    )


def exchange_code_for_user(*, settings: Settings, code: str) -> DiscordOAuthUser:
    token_payload = _discord_http_json(
        url="https://discord.com/api/v10/oauth2/token",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        data=urlencode(
            {
                "client_id": settings.discord_oauth_client_id,
                "client_secret": settings.discord_oauth_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.discord_oauth_redirect_url,
            }
        ).encode("utf-8"),
    )
    access_token = str(token_payload.get("access_token") or "").strip()
    if not access_token:
        raise DiscordOAuthError("Discord OAuth token exchange failed")
    user_payload = _discord_http_json(
        url="https://discord.com/api/v10/users/@me",
        method="GET",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        data=None,
    )
    discord_user_id = str(user_payload.get("id") or "").strip()
    if not discord_user_id:
        raise DiscordOAuthError("Discord OAuth user lookup failed")
    return DiscordOAuthUser(
        discord_user_id=discord_user_id,
        username=_optional_str(user_payload.get("username")),
        global_name=_optional_str(user_payload.get("global_name")),
        avatar_hash=_optional_str(user_payload.get("avatar")),
        access_token=access_token,
    )


def _discord_http_json(*, url: str, method: str, headers: dict[str, str], data: bytes | None) -> dict:
    request = Request(url=url, headers=headers, data=data, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8")
        raise DiscordOAuthError(f"Discord OAuth request failed ({exc.code}): {error_body}") from exc
    except URLError as exc:
        raise DiscordOAuthError(f"Discord OAuth request failed: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DiscordOAuthError("Discord OAuth response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise DiscordOAuthError("Discord OAuth response was not an object")
    return payload


def _sign_state(*, settings: Settings, encoded_payload: str) -> str:
    secret = settings.auth_token_secret.encode("utf-8")
    return hmac.new(secret, encoded_payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _urlsafe_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("utf-8").rstrip("=")


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _optional_str(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None
