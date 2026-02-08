from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class JiraOAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class JiraOAuthTokenSet:
    access_token: str
    refresh_token: str
    expires_at: datetime
    scopes: list[str]


@dataclass(frozen=True)
class JiraOAuthResource:
    cloud_id: str
    site_url: str
    name: str


@dataclass(frozen=True)
class JiraProject:
    key: str
    name: str


@dataclass(frozen=True)
class JiraIssuePreview:
    key: str
    summary: str
    status: str


@dataclass(frozen=True)
class JiraOAuthClientConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...] = ("read:jira-work", "write:jira-work", "manage:jira-webhook")


class JiraOAuthClient:
    def __init__(self, config: JiraOAuthClientConfig):
        self._config = config

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

    def _post_json(self, url: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            url=url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8")
            raise JiraOAuthError(f"Jira OAuth request failed ({exc.code}): {error_body}") from exc

        if not response_body:
            return {}
        return json.loads(response_body)

    def _get_json(self, url: str, *, access_token: str) -> dict | list:
        request = Request(
            url=url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8")
            raise JiraOAuthError(f"Jira API request failed ({exc.code}): {error_body}") from exc
        if not response_body:
            return {}
        return json.loads(response_body)

    def _request_json(
        self,
        *,
        method: str,
        url: str,
        access_token: str,
        payload: dict | None = None,
    ) -> dict | list:
        data: bytes | None = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        request = Request(
            url=url,
            data=data,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8")
            raise JiraOAuthError(f"Jira API request failed ({exc.code}): {error_body}") from exc
        if not response_body:
            return {}
        return json.loads(response_body)

    def _parse_tokens(self, payload: dict) -> JiraOAuthTokenSet:
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        scope_raw = payload.get("scope")

        if not isinstance(access_token, str) or not access_token:
            raise JiraOAuthError("Jira OAuth response missing access_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise JiraOAuthError("Jira OAuth response missing refresh_token")
        if not isinstance(expires_in, int):
            raise JiraOAuthError("Jira OAuth response missing expires_in")
        if not isinstance(scope_raw, str):
            scope_raw = ""

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(1, expires_in))
        scopes = [scope for scope in scope_raw.split(" ") if scope]
        return JiraOAuthTokenSet(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scopes=scopes,
        )

    def exchange_code(self, *, code: str) -> JiraOAuthTokenSet:
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

    def refresh_tokens(self, *, refresh_token: str) -> JiraOAuthTokenSet:
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

    def list_accessible_resources(self, *, access_token: str) -> list[JiraOAuthResource]:
        payload = self._get_json(
            "https://api.atlassian.com/oauth/token/accessible-resources",
            access_token=access_token,
        )
        if not isinstance(payload, list):
            raise JiraOAuthError("Accessible resources response was not a list")

        resources: list[JiraOAuthResource] = []
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
            resources.append(JiraOAuthResource(cloud_id=cloud_id, site_url=site_url, name=name))
        return resources

    def list_projects(self, *, access_token: str, cloud_id: str) -> list[JiraProject]:
        payload = self._get_json(
            f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/project/search?maxResults=100",
            access_token=access_token,
        )
        values = payload.get("values") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise JiraOAuthError("Project search response missing values list")

        projects: list[JiraProject] = []
        for item in values:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            name = item.get("name")
            if not isinstance(key, str) or not key:
                continue
            if not isinstance(name, str) or not name:
                name = key
            projects.append(JiraProject(key=key, name=name))
        projects.sort(key=lambda project: project.key)
        return projects

    def search_issues_by_jql(
        self,
        *,
        access_token: str,
        cloud_id: str,
        jql: str,
        max_results: int = 20,
    ) -> list[JiraIssuePreview]:
        bounded_max_results = max(1, min(max_results, 50))
        query = urlencode(
            {
                "jql": jql,
                "maxResults": bounded_max_results,
                "fields": "summary,status",
            }
        )
        payload = self._get_json(
            f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/search/jql?{query}",
            access_token=access_token,
        )
        issues = payload.get("issues") if isinstance(payload, dict) else None
        if not isinstance(issues, list):
            raise JiraOAuthError("Issue search response missing issues list")

        results: list[JiraIssuePreview] = []
        for item in issues:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            fields = item.get("fields")
            if not isinstance(fields, dict):
                fields = {}
            summary = fields.get("summary")
            status_obj = fields.get("status")
            status_name = status_obj.get("name") if isinstance(status_obj, dict) else None

            if not isinstance(key, str) or not key:
                continue
            if not isinstance(summary, str) or not summary:
                summary = key
            if not isinstance(status_name, str) or not status_name:
                status_name = "Unknown"

            results.append(JiraIssuePreview(key=key, summary=summary, status=status_name))
        return results

    def register_webhook(
        self,
        *,
        access_token: str,
        cloud_id: str,
        callback_url: str,
        jql_filter: str,
        events: list[str],
    ) -> list[int]:
        payload = self._request_json(
            method="POST",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/webhook",
            access_token=access_token,
            payload={
                "url": callback_url,
                "webhooks": [
                    {
                        "jqlFilter": jql_filter,
                        "events": events,
                    }
                ],
            },
        )
        webhook_ids = payload.get("createdWebhookId") if isinstance(payload, dict) else None
        if not isinstance(webhook_ids, list):
            raise JiraOAuthError("Webhook registration response missing createdWebhookId list")

        normalized_ids: list[int] = []
        for webhook_id in webhook_ids:
            if isinstance(webhook_id, int):
                normalized_ids.append(webhook_id)
            elif isinstance(webhook_id, str) and webhook_id.isdigit():
                normalized_ids.append(int(webhook_id))
        if not normalized_ids:
            raise JiraOAuthError("Webhook registration did not return any webhook IDs")
        return normalized_ids

    def delete_webhooks(
        self,
        *,
        access_token: str,
        cloud_id: str,
        webhook_ids: list[int],
    ) -> None:
        if not webhook_ids:
            return
        query = "&".join(f"webhookIds={webhook_id}" for webhook_id in webhook_ids)
        self._request_json(
            method="DELETE",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/webhook?{query}",
            access_token=access_token,
        )
