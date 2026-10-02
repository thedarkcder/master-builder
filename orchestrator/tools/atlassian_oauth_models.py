from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from typing import Any


class AtlassianOAuthError(RuntimeError):
    pass


class AtlassianOAuthHttpError(AtlassianOAuthError):
    def __init__(
        self, message: str, *, status_code: int, error_prefix: str, error_body: str
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_prefix = error_prefix
        self.error_body = error_body


class AtlassianOAuthAuthRequiredError(AtlassianOAuthError):
    pass


@dataclass(frozen=True)
class AtlassianOAuthTokenSet:
    access_token: str
    refresh_token: str
    expires_at: datetime
    scopes: list[str]


@dataclass(frozen=True)
class AtlassianOAuthResource:
    cloud_id: str
    site_url: str
    name: str


@dataclass(frozen=True)
class JiraProject:
    key: str
    name: str


@dataclass(frozen=True)
class ConfluenceSpace:
    space_id: str
    key: str
    name: str


@dataclass(frozen=True)
class ConfluencePage:
    page_id: str
    title: str
    webui_url: str


@dataclass(frozen=True)
class JiraIssuePreview:
    key: str
    summary: str
    status: str


@dataclass(frozen=True)
class JiraIssueSearchPage:
    issues: list[JiraIssuePreview]
    next_page_token: str | None = None


@dataclass(frozen=True)
class JiraIssueDetail:
    key: str
    summary: str
    status: str
    description: str
    status_category_key: str | None = None
    issue_type: str | None = None
    issue_type_hierarchy_level: int | None = None
    issue_type_is_subtask: bool | None = None
    labels: list[str] = field(default_factory=list)
    issue_id: str | None = None
    parent_key: str | None = None
    parent_issue_id: str | None = None


@dataclass(frozen=True)
class JiraIssueComment:
    comment_id: str
    body: str
    author_display_name: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class JiraIssueAttachment:
    attachment_id: str
    filename: str
    content_url: str
    mime_type: str | None = None
    size_bytes: int | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class JiraIssueCreateInput:
    summary: str
    description: str | dict[str, Any]
    labels: list[str]
    issue_type: str = "Task"
    parent_issue_key: str | None = None
    linked_parent_issue_key: str | None = None


@dataclass(frozen=True)
class JiraIssueCreateResult:
    key: str
    issue_id: str


@dataclass(frozen=True)
class JiraIssueBulkCreateResult:
    created: list[JiraIssueCreateResult]
    errors: list[str]


@dataclass(frozen=True)
class AtlassianOAuthClientConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...] = (
        "read:jira-work",
        "write:jira-work",
        "read:space:confluence",
        "read:page:confluence",
        "write:page:confluence",
        "offline_access",
        "manage:jira-webhook",
        "read:board-scope:jira-software",
        "read:board-scope.admin:jira-software",
        "read:jira-software",
        "read:project:jira",
        "read:sprint:jira-software",
        "read:issue-details:jira",
        "read:jql:jira",
        "read:attachment:jira",
        "write:attachment:jira",
    )
