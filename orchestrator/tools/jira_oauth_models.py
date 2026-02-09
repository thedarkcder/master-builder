from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


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
class JiraIssueDetail:
    key: str
    summary: str
    status: str
    description: str


@dataclass(frozen=True)
class JiraIssueCreateInput:
    summary: str
    description: str | dict[str, Any]
    labels: list[str]
    issue_type: str = "Task"


@dataclass(frozen=True)
class JiraIssueCreateResult:
    key: str
    issue_id: str


@dataclass(frozen=True)
class JiraIssueBulkCreateResult:
    created: list[JiraIssueCreateResult]
    errors: list[str]


@dataclass(frozen=True)
class JiraOAuthClientConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...] = ("read:jira-work", "write:jira-work", "offline_access", "manage:jira-webhook")
