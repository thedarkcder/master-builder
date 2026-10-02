from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode

from orchestrator.tools.atlassian_oauth_models import (
    AtlassianOAuthClientConfig,
    AtlassianOAuthError,
    AtlassianOAuthResource,
    AtlassianOAuthTokenSet,
)


class AtlassianOAuthCallbackFlow:
    def __init__(
        self,
        *,
        config: AtlassianOAuthClientConfig,
        post_json: Callable[[str, dict[str, Any]], dict[str, Any]],
        get_json: Callable[[str, str], dict[str, Any] | list[Any]],
    ) -> None:
        self._config = config
        self._post_json = post_json
        self._get_json = get_json

    def build_authorize_url(self, *, state: str) -> str:
        query = urlencode(
            {
                "audience": "api.atlassian.com",
                "client_id": self._config.client_id,
                "scope": " ".join(self._config.scopes),
                "redirect_uri": self._config.redirect_uri,
                "state": state,
                "response_type": "code",
                "prompt": "consent",
            }
        )
        return f"https://auth.atlassian.com/authorize?{query}"

    def exchange_code(self, *, code: str) -> AtlassianOAuthTokenSet:
        payload = self._post_json(
            "https://auth.atlassian.com/oauth/token",
            {
                "grant_type": "authorization_code",
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
                "code": code,
                "redirect_uri": self._config.redirect_uri,
            },
        )
        return self._parse_tokens(payload)

    def refresh_tokens(self, *, refresh_token: str) -> AtlassianOAuthTokenSet:
        payload = self._post_json(
            "https://auth.atlassian.com/oauth/token",
            {
                "grant_type": "refresh_token",
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
                "refresh_token": refresh_token,
            },
        )
        return self._parse_tokens(payload)

    def list_accessible_resources(
        self, *, access_token: str
    ) -> list[AtlassianOAuthResource]:
        payload = self._get_json(
            "https://api.atlassian.com/oauth/token/accessible-resources",
            access_token,
        )
        if not isinstance(payload, list):
            raise AtlassianOAuthError("Accessible resources response was not a list")

        resources: list[AtlassianOAuthResource] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            cloud_id = item.get("id")
            site_url = item.get("url")
            name = item.get("name")
            if not isinstance(cloud_id, str) or not cloud_id:
                continue
            if not isinstance(site_url, str) or not site_url:
                continue
            if not isinstance(name, str) or not name:
                name = site_url
            resources.append(
                AtlassianOAuthResource(cloud_id=cloud_id, site_url=site_url, name=name)
            )
        return resources

    def _parse_tokens(self, payload: dict[str, Any]) -> AtlassianOAuthTokenSet:
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        scope_raw = payload.get("scope")

        if not isinstance(access_token, str) or not access_token:
            raise AtlassianOAuthError("Atlassian response missing access_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise AtlassianOAuthError("Atlassian response missing refresh_token")
        if not isinstance(expires_in, int):
            raise AtlassianOAuthError("Atlassian response missing expires_in")
        if not isinstance(scope_raw, str):
            scope_raw = ""

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(1, expires_in))
        scopes = [scope for scope in scope_raw.split(" ") if scope]
        return AtlassianOAuthTokenSet(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scopes=scopes,
        )
