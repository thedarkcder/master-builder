from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import jwt


class GitHubApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubAppConfig:
    app_id: str
    installation_id: str
    private_key_pem: str
    api_base_url: str = "https://api.github.com"


@dataclass(frozen=True)
class PullRequestResult:
    number: int
    html_url: str


@dataclass
class _InstallationToken:
    token: str
    expires_at: datetime


def _parse_github_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def github_client_from_tenant_config(tenant_github_config: dict) -> "GitHubAppClient":
    mode = str(tenant_github_config.get("mode") or "")
    if mode != "github_app":
        raise ValueError("Only github_app mode is supported")

    app_id_ref = str(tenant_github_config.get("app_id_ref") or "")
    private_key_ref = str(tenant_github_config.get("private_key_ref") or "")
    installation_id = str(tenant_github_config.get("installation_id") or "")

    if not app_id_ref or not private_key_ref or not installation_id:
        raise ValueError("Missing github_app required config fields")

    app_id = os.environ.get(app_id_ref)
    if not app_id:
        raise ValueError(f"Missing GitHub App ID secret for ref '{app_id_ref}'")

    private_key_pem = os.environ.get(private_key_ref)
    if not private_key_pem:
        raise ValueError(f"Missing GitHub private key secret for ref '{private_key_ref}'")

    return GitHubAppClient(
        GitHubAppConfig(
            app_id=app_id,
            installation_id=installation_id,
            private_key_pem=private_key_pem,
        )
    )


class GitHubAppClient:
    def __init__(self, config: GitHubAppConfig):
        self._config = config
        self._cached_installation_token: _InstallationToken | None = None

    def create_app_jwt(self) -> str:
        now = datetime.now(timezone.utc)
        payload = {
            "iat": int((now - timedelta(seconds=60)).timestamp()),
            "exp": int((now + timedelta(minutes=9)).timestamp()),
            "iss": self._config.app_id,
        }
        encoded = jwt.encode(payload, self._config.private_key_pem, algorithm="RS256")
        return str(encoded)

    def _request_json(
        self,
        *,
        method: str,
        path: str,
        bearer_token: str,
        payload: dict | None = None,
    ) -> dict:
        url = f"{self._config.api_base_url.rstrip('/')}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {bearer_token}",
            "User-Agent": "master-builder-orchestrator",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url=url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8")
            raise GitHubApiError(
                f"GitHub API request failed ({exc.code}) for {method} {path}: {error_body}"
            ) from exc

        if not response_body:
            return {}
        return json.loads(response_body)

    def get_installation_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._cached_installation_token is not None:
            if self._cached_installation_token.expires_at - now > timedelta(seconds=60):
                return self._cached_installation_token.token

        app_jwt = self.create_app_jwt()
        response = self._request_json(
            method="POST",
            path=f"/app/installations/{self._config.installation_id}/access_tokens",
            bearer_token=app_jwt,
            payload={},
        )

        token = response.get("token")
        expires_at_raw = response.get("expires_at")
        if not isinstance(token, str) or not token:
            raise GitHubApiError("GitHub installation token response did not include token")
        if not isinstance(expires_at_raw, str) or not expires_at_raw:
            raise GitHubApiError("GitHub installation token response did not include expires_at")

        expires_at = _parse_github_datetime(expires_at_raw)
        self._cached_installation_token = _InstallationToken(token=token, expires_at=expires_at)
        return token

    def create_pull_request(
        self,
        *,
        repo_full_name: str,
        title: str,
        head_branch: str,
        base_branch: str,
        body: str,
    ) -> PullRequestResult:
        installation_token = self.get_installation_token()
        response = self._request_json(
            method="POST",
            path=f"/repos/{repo_full_name}/pulls",
            bearer_token=installation_token,
            payload={
                "title": title,
                "head": head_branch,
                "base": base_branch,
                "body": body,
            },
        )
        number = response.get("number")
        html_url = response.get("html_url")
        if not isinstance(number, int):
            raise GitHubApiError("GitHub PR response did not include numeric PR number")
        if not isinstance(html_url, str) or not html_url:
            raise GitHubApiError("GitHub PR response did not include html_url")

        return PullRequestResult(number=number, html_url=html_url)
