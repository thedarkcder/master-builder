from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from orchestrator.core.guardrails import enforce_safe_command


class JiraConfig(BaseModel):
    mcp_endpoint: str = Field(min_length=1)
    project_keys: list[str] = Field(default_factory=list, min_length=1)
    ready_label: str = Field(default="agent:ready", min_length=1)
    in_progress_label: str = Field(default="agent:in-progress", min_length=1)
    blocked_label: str = Field(default="agent:blocked", min_length=1)
    done_label: str | None = None
    webhook_secret_ref: str | None = None


class GithubConfig(BaseModel):
    mode: str = Field(default="github_app", min_length=1)
    webhook_secret_ref: str | None = None
    installation_id: str | None = None


class ReposConfig(BaseModel):
    allowlist: list[str] = Field(default_factory=list)
    mapping_rules_by_project_key: dict[str, str] = Field(default_factory=dict)
    mapping_rules_by_component: dict[str, str] = Field(default_factory=dict)
    fallback_repo: str | None = None


class PolicyConfig(BaseModel):
    allow_jira_transitions: bool = False
    allow_pr_creation: bool = True
    allow_label_mutations: bool = True
    max_runtime_minutes: int = 30
    max_dev_test_review_loops: int = 2
    max_concurrent_runs: int = 2
    allowed_commands: list[str] = Field(default_factory=list)
    require_agents_md: bool = False

    @field_validator("allowed_commands")
    @classmethod
    def validate_allowed_commands(cls, commands: list[str]) -> list[str]:
        for command in commands:
            try:
                enforce_safe_command(command)
            except (PermissionError, ValueError) as exc:
                raise ValueError(str(exc)) from exc
        return commands


class DiscordConfig(BaseModel):
    channel_id: str | None = None
    channel_name_template: str = "proj-{tenant_id}"
    notify_events: list[str] = Field(default_factory=list)


class TenantCreate(BaseModel):
    name: str = Field(min_length=1)
    is_enabled: bool = True
    jira: JiraConfig
    github: GithubConfig
    repos: ReposConfig
    policy: PolicyConfig
    discord: DiscordConfig | None = None


class TenantUpdate(BaseModel):
    name: str = Field(min_length=1)
    is_enabled: bool
    jira: JiraConfig
    github: GithubConfig
    repos: ReposConfig
    policy: PolicyConfig
    discord: DiscordConfig | None = None


class TenantRead(BaseModel):
    tenant_id: str
    name: str
    is_enabled: bool
    jira: JiraConfig
    github: GithubConfig
    repos: ReposConfig
    policy: PolicyConfig
    discord: DiscordConfig | None
    created_at: datetime
    updated_at: datetime


class IntegrationTestResult(BaseModel):
    ok: bool
    details: str


class GitHubInstallStart(BaseModel):
    install_url: str
    expires_at: datetime


class GitHubRepositoryRead(BaseModel):
    full_name: str
    html_url: str
    default_branch: str
    private: bool


class RunRead(BaseModel):
    run_id: str
    tenant_id: str
    issue_key: str
    repo_url: str | None
    branch: str | None
    pr_url: str | None
    status: str
    last_error: str | None
    plan: dict | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
