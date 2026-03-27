from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_serializer

from orchestrator.core.agent_execution_profiles import (
    normalize_execution_profile_routing,
    normalize_execution_profiles,
)
from orchestrator.core.codex_models import normalize_codex_model, normalize_codex_reasoning_effort
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
    allow_code_reviews: bool = True
    allow_pr_remediation: bool = True
    allow_manual_pr_fix_requests: bool = True
    allow_label_mutations: bool = True
    allow_auto_merge: bool = False
    max_dev_test_review_loops: int = 2
    max_pr_auto_remediation_loops: int = 5
    max_concurrent_runs: int = 2
    allowed_commands: list[str] = Field(default_factory=list)
    require_agents_md: bool = False
    knowledge_base_enabled: bool = True
    knowledge_auto_answer_mode: str = Field(default="aggressive", pattern="^(safe|balanced|aggressive)$")
    codex_model: str | None = None
    codex_reasoning_effort: str | None = Field(default=None, pattern="^(low|medium|high)$")
    execution_profiles: dict[str, dict[str, object]] = Field(default_factory=dict)
    execution_profile_routing: dict[str, str] = Field(default_factory=dict)

    @field_validator("allowed_commands")
    @classmethod
    def validate_allowed_commands(cls, commands: list[str]) -> list[str]:
        for command in commands:
            try:
                enforce_safe_command(command)
            except (PermissionError, ValueError) as exc:
                raise ValueError(str(exc)) from exc
        return commands

    @field_validator("codex_model")
    @classmethod
    def normalize_codex_model(cls, value: str | None) -> str | None:
        return normalize_codex_model(value)

    @field_validator("codex_reasoning_effort")
    @classmethod
    def normalize_codex_reasoning_effort(cls, value: str | None) -> str | None:
        return normalize_codex_reasoning_effort(value)

    @field_validator("execution_profiles")
    @classmethod
    def normalize_execution_profiles(cls, value: dict[str, dict[str, object]] | None) -> dict[str, dict[str, object]]:
        return normalize_execution_profiles(value)

    @field_validator("execution_profile_routing")
    @classmethod
    def normalize_execution_profile_routing(cls, value: dict[str, str] | None) -> dict[str, str]:
        return normalize_execution_profile_routing(value)


class DiscordConfig(BaseModel):
    guild_id: str | None = None
    channel_id: str | None = None
    channel_name_template: str = "proj-{tenant_id}"
    notify_events: list[str] = Field(default_factory=list)
    allowed_user_ids: list[str] = Field(default_factory=list)
    allowlist_requests: list[dict] = Field(default_factory=list)
    live_voice_enabled: bool = False
    live_voice_room_links: dict[str, str] = Field(default_factory=dict)
    voice_room_channel_ids: list[str] = Field(default_factory=list)
    voice_room_thread_channel_ids: list[str] = Field(default_factory=list)
    voice_thread_channel_ids: list[str] = Field(default_factory=list)
    persona_room_channel_ids: list[str] = Field(default_factory=list)
    persona_room_thread_channel_ids: list[str] = Field(default_factory=list)
    persona_thread_channel_ids: list[str] = Field(default_factory=list)
    room_channel_ids: list[str] = Field(default_factory=list)
    room_thread_channel_ids: list[str] = Field(default_factory=list)
    pm_room_channel_ids: list[str] = Field(default_factory=list)
    pm_room_thread_channel_ids: list[str] = Field(default_factory=list)
    pm_thread_channel_ids: list[str] = Field(default_factory=list)
    voice_room_channel_id: str | None = None
    voice_room_thread_channel_id: str | None = None
    voice_thread_channel_id: str | None = None
    persona_room_channel_id: str | None = None
    persona_room_thread_channel_id: str | None = None
    persona_thread_channel_id: str | None = None
    room_channel_id: str | None = None
    room_thread_channel_id: str | None = None
    pm_room_channel_id: str | None = None
    pm_room_thread_channel_id: str | None = None
    pm_thread_channel_id: str | None = None
    persona_names: dict[str, str] = Field(default_factory=dict)
    persona_voices: dict[str, str] = Field(default_factory=dict)
    voice_room_persona_names: dict[str, str] = Field(default_factory=dict)
    voice_room_persona_voices: dict[str, str] = Field(default_factory=dict)
    room_persona_names: dict[str, str] = Field(default_factory=dict)
    room_persona_voices: dict[str, str] = Field(default_factory=dict)
    pm_room_persona_names: dict[str, str] = Field(default_factory=dict)
    pm_room_persona_voices: dict[str, str] = Field(default_factory=dict)

    @model_serializer(mode="plain")
    def serialize_sparse(self) -> dict[str, object]:
        serialized: dict[str, object] = {}
        for field_name, field_info in type(self).model_fields.items():
            value = getattr(self, field_name)
            explicitly_set = field_name in self.model_fields_set
            if explicitly_set:
                serialized[field_name] = value
                continue
            default = field_info.default_factory() if field_info.default_factory is not None else field_info.default
            if value != default:
                serialized[field_name] = value
        return serialized


class ProjectDiscordConfig(BaseModel):
    channel_id: str | None = None
    notify_events: list[str] = Field(default_factory=list)
    ask_thread_channel_ids: list[str] = Field(default_factory=list)
    seed_followup_thread_channel_ids: list[str] = Field(default_factory=list)
    live_voice_enabled: bool = False
    live_voice_room_links: dict[str, str] = Field(default_factory=dict)
    voice_room_channel_ids: list[str] = Field(default_factory=list)
    voice_room_thread_channel_ids: list[str] = Field(default_factory=list)
    voice_thread_channel_ids: list[str] = Field(default_factory=list)
    persona_room_channel_ids: list[str] = Field(default_factory=list)
    persona_room_thread_channel_ids: list[str] = Field(default_factory=list)
    persona_thread_channel_ids: list[str] = Field(default_factory=list)
    room_channel_ids: list[str] = Field(default_factory=list)
    room_thread_channel_ids: list[str] = Field(default_factory=list)
    pm_room_channel_ids: list[str] = Field(default_factory=list)
    pm_room_thread_channel_ids: list[str] = Field(default_factory=list)
    pm_thread_channel_ids: list[str] = Field(default_factory=list)
    voice_room_channel_id: str | None = None
    voice_room_thread_channel_id: str | None = None
    voice_thread_channel_id: str | None = None
    persona_room_channel_id: str | None = None
    persona_room_thread_channel_id: str | None = None
    persona_thread_channel_id: str | None = None
    room_channel_id: str | None = None
    room_thread_channel_id: str | None = None
    pm_room_channel_id: str | None = None
    pm_room_thread_channel_id: str | None = None
    pm_thread_channel_id: str | None = None
    persona_names: dict[str, str] = Field(default_factory=dict)
    persona_voices: dict[str, str] = Field(default_factory=dict)
    voice_room_persona_names: dict[str, str] = Field(default_factory=dict)
    voice_room_persona_voices: dict[str, str] = Field(default_factory=dict)
    room_persona_names: dict[str, str] = Field(default_factory=dict)
    room_persona_voices: dict[str, str] = Field(default_factory=dict)
    pm_room_persona_names: dict[str, str] = Field(default_factory=dict)
    pm_room_persona_voices: dict[str, str] = Field(default_factory=dict)

    @model_serializer(mode="plain")
    def serialize_sparse(self) -> dict[str, object]:
        serialized: dict[str, object] = {}
        for field_name, field_info in type(self).model_fields.items():
            value = getattr(self, field_name)
            explicitly_set = field_name in self.model_fields_set
            if explicitly_set:
                serialized[field_name] = value
                continue
            default = field_info.default_factory() if field_info.default_factory is not None else field_info.default
            if value != default:
                serialized[field_name] = value
        return serialized


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


class KnowledgeAssetCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    source_type: str = Field(default="manual", min_length=1, max_length=32)
    mime_type: str | None = Field(default=None, max_length=128)
    source_ref: str | None = Field(default=None, max_length=1024)
    source_timestamp: str | None = None
    text_content: str | None = None
    content_base64: str | None = None


class KnowledgeAssetRead(BaseModel):
    asset_id: str
    tenant_id: str
    project_id: str
    source_type: str
    title: str
    mime_type: str | None = None
    source_ref: str | None = None
    source_timestamp: datetime | None = None
    chunk_count: int
    status: str
    created_at: datetime
    updated_at: datetime


class KnowledgeSourceCreate(BaseModel):
    connector_type: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, max_length=255)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    sync_mode: str | None = Field(default=None, pattern="^(manual|scheduled)$")
    config_json: dict = Field(default_factory=dict)


class KnowledgeSourceUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=255)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    sync_mode: str | None = Field(default=None, pattern="^(manual|scheduled)$")
    config_json: dict | None = None


class KnowledgeSourceRead(BaseModel):
    source_id: str
    tenant_id: str
    project_id: str
    connector_type: str
    display_name: str
    status: str
    sync_mode: str
    config_json: dict = Field(default_factory=dict)
    config_summary: str
    supports_sync_now: bool
    supports_scheduled_sync: bool
    last_synced_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class KnowledgeFactRead(BaseModel):
    fact_id: str
    asset_id: str
    chunk_id: str | None = None
    tenant_id: str
    project_id: str
    fact_type: str
    fact_key: str
    fact_value: str
    approval_state: str
    confidence: float
    is_inferred: bool
    metadata_json: dict = Field(default_factory=dict)
    source_timestamp: datetime | None = None
    superseded_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class KnowledgeAssetDetailRead(KnowledgeAssetRead):
    text_content: str | None = None
    metadata_json: dict = Field(default_factory=dict)
    facts: list[KnowledgeFactRead] = Field(default_factory=list)


class KnowledgeChunkRead(BaseModel):
    chunk_id: str
    asset_id: str
    tenant_id: str
    project_id: str
    chunk_index: int
    content: str
    token_count: int
    source_timestamp: datetime | None = None
    created_at: datetime
    updated_at: datetime


class KnowledgeAssetPageRead(BaseModel):
    items: list[KnowledgeAssetRead] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class KnowledgeSourcePageRead(BaseModel):
    items: list[KnowledgeSourceRead] = Field(default_factory=list)
    total: int


class KnowledgeChunkPageRead(BaseModel):
    items: list[KnowledgeChunkRead] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class KnowledgeDebugMatchRead(BaseModel):
    layer: str
    score: float
    asset_id: str
    source_type: str
    title: str
    source_ref: str | None = None
    source_timestamp: str | None = None
    fact_id: str | None = None
    chunk_id: str | None = None
    snippet: str
    metadata: dict = Field(default_factory=dict)


class KnowledgeDebugSearchRead(BaseModel):
    query: str
    items: list[KnowledgeDebugMatchRead] = Field(default_factory=list)


class KnowledgeAssetStatsRead(BaseModel):
    total_assets: int
    total_chunks: int
    total_facts: int
    approved_facts: int
    pending_review_facts: int
    superseded_facts: int
    ready_assets: int
    pending_review_assets: int
    rejected_assets: int
    latest_asset_updated_at: datetime | None = None
    source_type_counts: dict[str, int] = Field(default_factory=dict)


class KnowledgeAssetStatusUpdate(BaseModel):
    status: str = Field(pattern="^(pending_review|ready|rejected)$")


class KnowledgeSyncResultRead(BaseModel):
    ok: bool
    synced_assets: int
    skipped_assets: int
    created_assets: int = 0
    updated_assets: int = 0
    unchanged_assets: int = 0
    deleted_assets: int = 0
    failed_assets: int = 0
    details: str | None = None


class IntegrationTestResult(BaseModel):
    ok: bool
    details: str


class CodexModelOptionRead(BaseModel):
    id: str
    label: str
    description: str | None = None


class CodexReasoningOptionRead(BaseModel):
    id: str
    label: str
    description: str | None = None


class CodexModelCatalogRead(BaseModel):
    default_model: str
    default_reasoning_effort: str
    models: list[CodexModelOptionRead] = Field(default_factory=list)
    reasoning_efforts: list[CodexReasoningOptionRead] = Field(default_factory=list)


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
    dev_session_id: str | None = None
    pm_session_id: str | None = None
    orchestrated_session_id: str | None = None
    status: str
    last_error: str | None
    plan: dict | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunRerunRequest(BaseModel):
    mode: str = Field(default="fresh", pattern="^(fresh|resume)$")
    resume_stage: str | None = Field(default=None, pattern="^(orchestrated|pm|dev|review)$")

    @field_validator("resume_stage")
    @classmethod
    def validate_resume_stage(cls, value: str | None, info):  # type: ignore[override]
        mode = info.data.get("mode")
        if mode == "resume" and not value:
            raise ValueError("resume_stage is required when mode=resume")
        if mode != "resume" and value is not None:
            raise ValueError("resume_stage is only allowed when mode=resume")
        return value


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


class KnowledgeJiraSyncProjectStatusRead(BaseModel):
    tenant_id: str
    project_id: str
    jira_project_key: str
    state: str
    failure_category: str | None = None
    last_error: str | None = None
    last_attempted_at: datetime | None = None
    last_successful_sync_at: datetime | None = None
    next_retry_at: datetime | None = None
    consecutive_failures: int


class KnowledgeJiraSyncRuntimeRead(BaseModel):
    state: str
    enabled: bool
    database_backend: str
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    last_pass_started_at: datetime | None = None
    last_pass_finished_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    leader_acquired: bool
    service_instance_id: str | None = None
    stale: bool = False
    projects: list[KnowledgeJiraSyncProjectStatusRead] = Field(default_factory=list)


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


class DiscordCommandSyncStatusRead(BaseModel):
    synced: bool
    healthy: bool
    interaction_ingress_ready: bool
    bot_token_configured: bool
    guild_id_configured: bool
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_reason: str | None = None
    last_error: str | None = None
    guild_id: str | None = None
    application_id: str | None = None
    command_count: int


class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class AdminLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AdminIdentityResponse(BaseModel):
    username: str


class TokenTimelineTurnRead(BaseModel):
    turn_id: str
    invocation_id: str
    stage: str
    attempt: int | None
    recorded_at: datetime
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    delta_input: int
    delta_uncached: int
    delta_output: int
    runtime_ms: int | None
    is_growth_spike: bool
    spike_reason: list[str] = Field(default_factory=list)


class TokenTimelineTotalsRead(BaseModel):
    input: int
    uncached_input: int
    output: int
    cached_input: int
    cache_ratio: float
    total_io: int
    avg_runtime_ms: float
    p95_runtime_ms: float


class TokenTimelineRead(BaseModel):
    run_id: str
    issue_key: str
    model: str | None
    status: str
    totals: TokenTimelineTotalsRead
    turns: list[TokenTimelineTurnRead]


class TokenOverviewKpiRead(BaseModel):
    total_input: int
    total_uncached_input: int
    total_output: int
    total_io: int
    cache_ratio: float
    avg_runtime_ms: float
    p95_runtime_ms: float
    avg_io_per_run: float
    p95_io_per_run: float
    retest_waste_score: float


class TokenOverviewSeriesByDayRead(BaseModel):
    day: str
    total_input: int
    total_uncached_input: int
    total_output: int
    total_io: int
    delta_input: int
    delta_uncached: int
    delta_output: int
    delta_total_io: int
    avg_runtime_ms: float
    p95_runtime_ms: float
    run_count: int


class TokenOverviewTopRunRead(BaseModel):
    run_id: str
    issue_key: str
    tenant_id: str
    project_id: str | None
    status: str
    input: int
    uncached_input: int
    output: int
    total_io: int
    delta_total_io: int
    cache_ratio: float


class TokenAlertRead(BaseModel):
    rule: str
    severity: str = "warning"
    run_id: str
    turn_id: str | None = None
    stage: str | None = None
    attempt: int | None = None
    value: float | int | None = None
    threshold: float | int | None = None
    message: str


class TokenOverviewRead(BaseModel):
    kpis: TokenOverviewKpiRead
    series_by_day: list[TokenOverviewSeriesByDayRead]
    top_costly_runs: list[TokenOverviewTopRunRead]
    alerts: list[TokenAlertRead] = Field(default_factory=list)


class TokenCompareRunTotalsRead(BaseModel):
    input: int
    uncached_input: int
    output: int
    cached_input: int
    total_io: int
    cache_ratio: float


class TokenCompareStageTotalsRead(BaseModel):
    stage: str
    input: int
    uncached_input: int
    output: int
    total_io: int


class TokenCompareRunRead(BaseModel):
    run_id: str
    issue_key: str
    status: str | None
    totals: TokenCompareRunTotalsRead
    stage_totals: list[TokenCompareStageTotalsRead]


class TokenCompareWaterfallStepRead(BaseModel):
    run_id: str
    turn_order: int
    turn_id: str
    recorded_at: datetime
    stage: str
    attempt: int | None
    delta_input: int
    uncached_delta: int
    output_tokens: int
    delta_reason: list[str] = Field(default_factory=list)


class TokenCompareRead(BaseModel):
    runs: list[TokenCompareRunRead]
    align_axis: list[int]
    waterfall: list[TokenCompareWaterfallStepRead]


class TokenCompareRequest(BaseModel):
    run_ids: list[str]
    align_by: str = "turn_sequence"


class TokenStageDiagnosticRead(BaseModel):
    stage: str
    avg_delta: float
    avg_uncached_delta: float
    retry_impact_index: float
    run_count: int


class TokenStageHeatmapCellRead(BaseModel):
    stage: str
    attempt: int
    avg_delta: float
    avg_uncached_delta: float
    sample_count: int


class TokenHeavyCommandRead(BaseModel):
    command_signature: str
    stage: str
    spike_count: int
    avg_delta: float
    avg_uncached_delta: float


class TokenScatterPointRead(BaseModel):
    run_id: str
    issue_key: str
    stage: str
    attempt: int | None
    runtime_ms: int | None
    token_delta: int
    recorded_at: datetime


class TokenIssueStageUsageRead(BaseModel):
    issue_key: str
    stage: str
    input: int
    uncached_input: int
    output: int
    total_io: int
    delta_total_io: int
    run_count: int


class TokenStageDiagnosticsRead(BaseModel):
    stages: list[TokenStageDiagnosticRead]
    heavy_commands: list[TokenHeavyCommandRead]
    scatter_points: list[TokenScatterPointRead]
    heatmap: list[TokenStageHeatmapCellRead] = Field(default_factory=list)
    retest_waste_score: float = 0.0
    issue_stage_totals: list[TokenIssueStageUsageRead] = Field(default_factory=list)


class TokenProjectStageDiagnosticsRead(BaseModel):
    project_id: str
    project_name: str | None
    stages: list[TokenStageDiagnosticRead]
    retest_waste_score: float = 0.0


class TokenStageDiagnosticsCompareRead(BaseModel):
    projects: list[TokenProjectStageDiagnosticsRead]
