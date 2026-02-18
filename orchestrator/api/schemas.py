from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from orchestrator.core.guardrails import enforce_safe_command


class JiraConfig(BaseModel):
    connection_id: str | None = None
    project_keys: list[str] = Field(default_factory=list)
    ready_statuses: list[str] = Field(default_factory=lambda: ["Ready for Agent"], min_length=1)
    ready_trigger_mode: str = Field(default="status_recheck", pattern="^(status_recheck|transition_only)$")
    ready_jql: str | None = None
    ready_label: str = Field(default="agent:ready", min_length=1)
    in_progress_label: str = Field(default="agent:in-progress", min_length=1)
    blocked_label: str = Field(default="agent:blocked", min_length=1)
    done_label: str | None = None
    webhook_secret_ref: str | None = None
    managed_webhook_ids: list[int] = Field(default_factory=list)
    webhook_last_provisioned_at: str | None = None
    webhook_last_error: str | None = None
    webhook_last_received_at: str | None = None
    webhook_last_delivery_id: str | None = None
    webhook_last_issue_key: str | None = None
    webhook_last_event: str | None = None


class GithubConfig(BaseModel):
    webhook_secret_ref: str | None = None
    installation_id: str | None = None


class ReposConfig(BaseModel):
    github_repository: str | None = None
    allowlist: list[str] = Field(default_factory=list)
    mapping_rules_by_project_key: dict[str, str] = Field(default_factory=dict)
    mapping_rules_by_component: dict[str, str] = Field(default_factory=dict)


class PolicyConfig(BaseModel):
    allow_jira_transitions: bool = False
    allow_pr_creation: bool = True
    allow_label_mutations: bool = True
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
    guild_id: str | None = None
    channel_id: str | None = None
    channel_name_template: str = "proj-{tenant_id}"
    notify_events: list[str] = Field(default_factory=list)
    allowed_user_ids: list[str] = Field(default_factory=list)
    allowlist_requests: list[dict] = Field(default_factory=list)


class ProjectDiscordConfig(BaseModel):
    channel_id: str | None = None
    notify_events: list[str] = Field(default_factory=list)
    ask_thread_channel_ids: list[str] = Field(default_factory=list)
    seed_followup_thread_channel_ids: list[str] = Field(default_factory=list)


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


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    github_repository: str = Field(min_length=1)
    jira_project_key: str = Field(min_length=1)
    policy_overrides: dict = Field(default_factory=dict)
    environment: dict[str, str] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    discord: ProjectDiscordConfig | None = None


class ProjectUpdate(BaseModel):
    name: str = Field(min_length=1)
    github_repository: str = Field(min_length=1)
    jira_project_key: str = Field(min_length=1)
    policy_overrides: dict = Field(default_factory=dict)
    environment: dict[str, str] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    discord: ProjectDiscordConfig | None = None
    is_archived: bool = False


class ProjectRead(BaseModel):
    project_id: str
    tenant_id: str
    name: str
    github_repository: str
    jira_project_key: str
    policy_overrides: dict = Field(default_factory=dict)
    environment: dict[str, str] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    discord: ProjectDiscordConfig | None = None
    effective_policy: PolicyConfig
    is_archived: bool
    created_at: datetime
    updated_at: datetime


class IntegrationTestResult(BaseModel):
    ok: bool
    details: str


class GitHubInstallStart(BaseModel):
    install_url: str
    expires_at: datetime


class JiraConnectStart(BaseModel):
    authorize_url: str
    expires_at: datetime


class JiraProjectRead(BaseModel):
    key: str
    name: str


class JiraWebhookActionResult(BaseModel):
    ok: bool
    action: str
    details: str
    webhook_ids: list[int] = Field(default_factory=list)


class JiraWebhookDiagnosticsRead(BaseModel):
    tenant_id: str
    connected: bool
    webhook_url: str
    managed_webhook_ids: list[int] = Field(default_factory=list)
    last_provisioned_at: str | None = None
    last_received_at: str | None = None
    last_delivery_id: str | None = None
    last_issue_key: str | None = None
    last_error: str | None = None
    recent_delivery_window_minutes: int
    recent_delivery_ok: bool


class ReadyIssuePreviewRead(BaseModel):
    key: str
    summary: str
    status: str


class ReadyGatePreviewRead(BaseModel):
    ready_statuses: list[str]
    ready_jql: str
    eligible_issues: list[ReadyIssuePreviewRead]
    guidance: str


class GitHubRepositoryRead(BaseModel):
    full_name: str
    html_url: str
    default_branch: str
    private: bool


class RepoBootstrapStateRead(BaseModel):
    tenant_id: str
    repo_url: str
    bootstrap_count: int
    last_created_files: list[str]
    bootstrapped_at: datetime
    updated_at: datetime


class ReleaseBootstrapReportRead(BaseModel):
    tenant_id: str
    ok: bool
    checks: dict[str, bool] = Field(default_factory=dict)
    details: list[str] = Field(default_factory=list)
    checked_at: str


class ManagedSecretUpsert(BaseModel):
    value: str = Field(min_length=1)


class ManagedSecretRead(BaseModel):
    secret_ref: str
    source: str
    updated_at: datetime | None


class ManagedSecretResolveRequest(BaseModel):
    secret_ref: str = Field(min_length=1)


class ManagedSecretResolveResult(BaseModel):
    secret_ref: str
    source: str
    resolved: bool


class RunRead(BaseModel):
    run_id: str
    tenant_id: str
    project_id: str | None
    issue_key: str
    issue_summary: str | None = None
    issue_url: str | None = None
    repo_url: str | None
    branch: str | None
    pr_url: str | None
    codex_session_id: str | None = None
    pm_session_id: str | None = None
    status: str
    last_error: str | None
    plan: dict | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunEventRead(BaseModel):
    event_type: str
    run_id: str
    issue_key: str | None = None
    project_id: str | None = None
    agent_id: str
    recorded_at: datetime


class RunLogEventRead(BaseModel):
    run_id: str | None = None
    issue_key: str | None = None
    project_id: str | None = None
    agent_id: str
    invocation_id: str | None = None
    channel: str | None = None
    command: str | None = None
    working_dir: str | None = None
    stage: str
    attempt: int | None = None
    stream: str
    message: str
    recorded_at: datetime


class AgentEventRead(BaseModel):
    event_type: str
    run_id: str
    issue_key: str | None = None
    project_id: str | None = None
    recorded_at: datetime


class AgentActivityRead(BaseModel):
    tenant_id: str
    agent_id: str
    last_seen_at: datetime
    is_dark: bool
    events: list[AgentEventRead] = Field(default_factory=list)


class ProjectExecutionMetricsRead(BaseModel):
    tenant_id: str
    project_id: str
    tasks_started: int
    tasks_completed: int
    tasks_failed: int
    tasks_blocked: int
    success_rate_ratio: float
    average_duration_seconds: float
    median_duration_seconds: float
    p95_duration_seconds: float
    queue_length: int
    average_time_in_queue_seconds: float
    stale_queued_tasks: int
    sla_breaches: int


class AlertRead(BaseModel):
    alert_key: str
    severity: str
    scope_type: str
    scope_id: str | None = None
    reason: str
    emitted_at: datetime


class AlertEvaluationRead(BaseModel):
    evaluated_at: datetime
    cooldown_seconds: int
    alerts: list[AlertRead] = Field(default_factory=list)


class TenantIntegrationHealthRead(BaseModel):
    jira_connected: bool
    github_connected: bool
    jira_webhook_healthy: bool


class TenantHealthRead(BaseModel):
    tenant_id: str
    active_projects: int
    active_agents: int
    total_runs: int
    failed_runs: int
    run_failure_rate_ratio: float
    average_task_duration_seconds: float
    webhook_events_received: int
    webhook_events_failed: int
    webhook_failure_rate_ratio: float
    integrations: TenantIntegrationHealthRead


class ObservabilityDurationStatsRead(BaseModel):
    average_seconds: float
    median_seconds: float
    p95_seconds: float


class PlatformObservabilityRead(BaseModel):
    total_tenants: int
    enabled_tenants: int
    total_projects: int
    active_projects: int
    total_runs: int
    active_runs: int
    failed_runs_last_24h: int
    run_duration: ObservabilityDurationStatsRead


class TenantObservabilityRead(BaseModel):
    tenant_id: str
    total_projects: int
    active_projects: int
    total_runs: int
    queued_runs: int
    running_runs: int
    succeeded_runs: int
    failed_runs: int
    blocked_runs: int
    stale_runs: int
    run_duration: ObservabilityDurationStatsRead


class ProjectObservabilityRead(BaseModel):
    tenant_id: str
    project_id: str
    total_runs: int
    queued_runs: int
    running_runs: int
    succeeded_runs: int
    failed_runs: int
    blocked_runs: int
    stale_runs: int
    run_duration: ObservabilityDurationStatsRead


class DiscordCommandRequest(BaseModel):
    user_id: str = Field(min_length=1)
    command: str = Field(min_length=2)
    channel_id: str | None = None
    command_params: dict[str, str] | None = None
    attachments: list[dict[str, str]] = Field(default_factory=list)


class DiscordCommandResponse(BaseModel):
    ok: bool
    command: str
    message: str
    data: dict | None = None


class DiscordAllowlistRequestRead(BaseModel):
    project_id: str | None = None
    user_id: str
    requested_at: str
    channel_id: str | None = None
    reason: str | None = None
    permissions: list[str] = Field(default_factory=list)


class DiscordAllowlistApprovalResult(BaseModel):
    ok: bool
    details: str
    project_id: str | None = None
    user_id: str
    notified: bool


class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class AdminLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AdminIdentityResponse(BaseModel):
    username: str
