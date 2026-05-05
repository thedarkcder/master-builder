from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

from orchestrator.core.runtime.agent_execution_profiles import (
    normalize_execution_profile_routing,
    normalize_execution_profiles,
)
from orchestrator.core.runtime.models import normalize_codex_model, normalize_codex_reasoning_effort
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


class ObservabilityPolicyConfig(BaseModel):
    audit_retention_days: int = Field(default=365, ge=1, le=3650)
    audit_export_enabled: bool = True
    legal_hold_enabled: bool = False
    legal_hold_reason: str | None = Field(default=None, max_length=500)

    @field_validator("legal_hold_reason")
    @classmethod
    def normalize_legal_hold_reason(cls, value: str | None) -> str | None:
        return str(value or "").strip() or None

    @model_validator(mode="after")
    def validate_legal_hold(self) -> "ObservabilityPolicyConfig":
        if self.legal_hold_enabled and not self.legal_hold_reason:
            raise ValueError("legal_hold_reason is required when legal_hold_enabled is true")
        if not self.legal_hold_enabled:
            self.legal_hold_reason = None
        return self


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
    observability: ObservabilityPolicyConfig = Field(default_factory=ObservabilityPolicyConfig)

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
    installed_at: str | None = None
    installer_user_id: str | None = None
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
    onboarding_channel_id: str | None = None
    onboarding_invite_expires_in_seconds: int | None = None
    onboarding_invite_max_uses: int | None = None

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


class ProjectArchitectureDocsConfig(BaseModel):
    provider: Literal["internal", "confluence"]
    space_key: str | None = None
    parent_page_id: str | None = None

    @field_validator("space_key", "parent_page_id")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None


class ConfluenceSpaceRead(BaseModel):
    space_id: str
    key: str
    name: str


class ConfluenceSpaceCatalogRead(BaseModel):
    items: list[ConfluenceSpaceRead]
    create_space_url: str


class ConfluencePageRead(BaseModel):
    page_id: str
    title: str
    webui_url: str


class TenantCreate(BaseModel):
    name: str = Field(min_length=1)
    is_enabled: bool = True
    jira: JiraConfig
    github: GithubConfig
    repos: ReposConfig
    policy: PolicyConfig
    discord: DiscordConfig | None = None
    experience: dict = Field(default_factory=lambda: {"default_mode": "technical"})
    setup_state: dict = Field(default_factory=dict)


class TenantUpdate(BaseModel):
    name: str = Field(min_length=1)
    is_enabled: bool
    jira: JiraConfig
    github: GithubConfig
    repos: ReposConfig
    policy: PolicyConfig
    discord: DiscordConfig | None = None
    experience: dict = Field(default_factory=lambda: {"default_mode": "technical"})
    setup_state: dict = Field(default_factory=dict)


class TenantRead(BaseModel):
    tenant_id: str
    name: str
    is_enabled: bool
    archived_at: datetime | None = None
    purge_after_at: datetime | None = None
    jira: JiraConfig
    github: GithubConfig
    repos: ReposConfig
    policy: PolicyConfig
    discord: DiscordConfig | None
    experience: dict = Field(default_factory=dict)
    setup_state: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class TenantMembershipIdentityRead(BaseModel):
    membership_id: str
    tenant_id: str
    role: str
    permission_keys: list[str] = Field(default_factory=list)
    effective_mode: str
    mode_override: str | None = None
    onboarding_kind: str
    first_signed_in_at: datetime | None = None
    onboarding_completed_at: datetime | None = None
    onboarding_version: str | None = None
    team_ids: list[str] = Field(default_factory=list)
    discord_state: dict = Field(default_factory=dict)


class AuthenticatedPrincipalRead(BaseModel):
    principal_type: str
    username: str | None = None
    user_id: str | None = None
    email: str | None = None
    full_name: str | None = None
    memberships: list[TenantMembershipIdentityRead] = Field(default_factory=list)


class TenantUserLoginRequest(BaseModel):
    email: str = Field(min_length=1)
    password: str = Field(min_length=1)


class TenantUserLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    principal: AuthenticatedPrincipalRead


class PublicRegistrationRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=8)
    tenant_name: str = Field(min_length=1, max_length=255)


class PublicRegistrationResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    principal: AuthenticatedPrincipalRead
    tenant: TenantRead


class TenantInviteCreate(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    full_name: str | None = Field(default=None, max_length=255)
    role: str = Field(pattern="^(tenant_admin|technical_member|business_member)$")
    team_ids: list[str] = Field(default_factory=list)
    mode_override: str | None = Field(default=None, pattern="^(technical|non_technical)$")


class TenantInviteRead(BaseModel):
    invite_id: str
    tenant_id: str
    email: str
    full_name: str | None = None
    role: str
    team_ids: list[str] = Field(default_factory=list)
    mode_override: str | None = None
    status: str
    invite_url: str | None = None
    expires_at: datetime
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TenantInviteActionResult(BaseModel):
    invite: TenantInviteRead


class TenantInviteListRead(BaseModel):
    items: list[TenantInviteRead] = Field(default_factory=list)


class TenantTeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    permission_keys: list[str] = Field(default_factory=list)


class TenantTeamUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    permission_keys: list[str] = Field(default_factory=list)


class TenantTeamRead(BaseModel):
    team_id: str
    tenant_id: str
    name: str
    description: str | None = None
    permission_keys: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class TenantMemberRead(BaseModel):
    membership_id: str
    tenant_id: str
    user_id: str
    email: str
    full_name: str | None = None
    is_active: bool
    role: str
    permission_keys: list[str] = Field(default_factory=list)
    effective_mode: str
    mode_override: str | None = None
    onboarding_kind: str
    first_signed_in_at: datetime | None = None
    onboarding_completed_at: datetime | None = None
    onboarding_version: str | None = None
    team_ids: list[str] = Field(default_factory=list)
    discord_state: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class TenantMemberUpdate(BaseModel):
    role: str = Field(pattern="^(tenant_admin|technical_member|business_member)$")
    team_ids: list[str] = Field(default_factory=list)
    mode_override: str | None = Field(default=None, pattern="^(technical|non_technical)$")
    is_active: bool = True


class TenantDiscordLinkStartRead(BaseModel):
    authorize_url: str


class TenantDiscordIdentityRead(BaseModel):
    oauth_configured: bool = False
    linked: bool
    discord_user_id: str | None = None
    discord_username: str | None = None
    discord_global_name: str | None = None
    discord_avatar_hash: str | None = None
    linked_at: datetime | None = None


class TenantDiscordInviteRead(BaseModel):
    invite_url: str
    expires_at: datetime | None = None
    max_uses: int | None = None


class DeliverySummaryAggregateRead(BaseModel):
    completed_count: int
    in_review_count: int
    blocked_count: int
    failed_count: int
    queued_count: int
    median_cycle_time_hours: float | None = None
    average_cycle_time_hours: float | None = None


class DeliveryTimelineItemRead(BaseModel):
    run_id: str
    project_id: str | None = None
    issue_key: str
    issue_summary: str | None = None
    status: str
    completed_at: datetime | None = None
    started_at: datetime | None = None
    pr_url: str | None = None


class TenantDeliverySummaryRead(BaseModel):
    summary: DeliverySummaryAggregateRead
    timeline: list[DeliveryTimelineItemRead] = Field(default_factory=list)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    github_repository: str = Field(min_length=1)
    jira_project_key: str = Field(min_length=1)
    policy_overrides: dict = Field(default_factory=dict)
    architecture_docs: ProjectArchitectureDocsConfig | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    discord: ProjectDiscordConfig | None = None


class ProjectUpdate(BaseModel):
    name: str = Field(min_length=1)
    github_repository: str = Field(min_length=1)
    jira_project_key: str = Field(min_length=1)
    policy_overrides: dict = Field(default_factory=dict)
    architecture_docs: ProjectArchitectureDocsConfig | None = None
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
    architecture_docs: ProjectArchitectureDocsConfig | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    discord: ProjectDiscordConfig | None = None
    effective_policy: PolicyConfig
    is_archived: bool
    created_at: datetime
    updated_at: datetime


class ArchitectureDocumentCreate(BaseModel):
    parent_issue_key: str = Field(min_length=1)
    issue_summary: str | None = None
    title: str | None = None
    canonical_url: str | None = None
    provider_ref: str | None = None


class ArchitectureDocumentUpdate(BaseModel):
    title: str = Field(min_length=1)
    status: Literal["draft", "ready", "superseded"]
    content_markdown: str | None = None
    canonical_url: str | None = None
    provider_ref: str | None = None


class ArchitectureDocumentRead(BaseModel):
    document_id: str
    tenant_id: str
    project_id: str
    parent_issue_key: str
    provider: Literal["internal", "confluence"]
    title: str
    status: Literal["draft", "ready", "superseded"]
    is_active: bool
    canonical_url: str
    provider_ref: str | None = None
    knowledge_asset_id: str | None = None
    content_markdown: str | None = None
    metadata: dict = Field(default_factory=dict)
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime
    updated_at: datetime


class ArchitectureDocumentPageRead(BaseModel):
    items: list[ArchitectureDocumentRead] = Field(default_factory=list)
    total: int


class ProjectInstallWrite(BaseModel):
    kind: str = Field(min_length=1)
    label: str = Field(min_length=1)
    enabled: bool = True
    config: dict = Field(default_factory=dict)
    binding_names: list[str] = Field(default_factory=list)


class ProjectInstallRead(BaseModel):
    install_id: str
    tenant_id: str
    project_id: str
    kind: str
    label: str
    enabled: bool
    config: dict = Field(default_factory=dict)
    binding_names: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ProjectInstallsRead(BaseModel):
    installs: list[ProjectInstallRead] = Field(default_factory=list)


class ProjectInstallRequestRead(BaseModel):
    request_id: str
    tenant_id: str
    project_id: str
    workflow_id: str | None = None
    run_id: str | None = None
    issue_key: str
    kind: str
    label: str
    reason: str
    suggested_config: dict = Field(default_factory=dict)
    required_bindings: list[str] = Field(default_factory=list)
    status: str
    request_kind: str
    created_at: datetime
    updated_at: datetime


class ProjectInstallRequestsRead(BaseModel):
    requests: list[ProjectInstallRequestRead] = Field(default_factory=list)


class ProjectInstallRequestUpdate(BaseModel):
    status: str = Field(min_length=1)


class ProjectAutomationWrite(BaseModel):
    kind: str = Field(min_length=1)
    enabled: bool = True
    timezone: str = Field(min_length=1)
    days_of_week: list[int | str] = Field(default_factory=list)
    local_time: str = Field(min_length=1)
    fallback_lookback_hours: int = Field(default=24, ge=1)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("timezone is required")
        return normalized

    @field_validator("days_of_week")
    @classmethod
    def validate_days_of_week(cls, value: list[int | str]) -> list[int]:
        normalized: list[int] = []
        seen: set[int] = set()
        weekday_aliases = {
            "mon": 0,
            "monday": 0,
            "tue": 1,
            "tues": 1,
            "tuesday": 1,
            "wed": 2,
            "wednesday": 2,
            "thu": 3,
            "thur": 3,
            "thurs": 3,
            "thursday": 3,
            "fri": 4,
            "friday": 4,
            "sat": 5,
            "saturday": 5,
            "sun": 6,
            "sunday": 6,
        }
        for raw_value in value:
            if isinstance(raw_value, bool):
                raise ValueError("days_of_week must contain integers 0-6 or weekday names")
            if isinstance(raw_value, int):
                day = raw_value
            else:
                normalized_value = str(raw_value or "").strip().lower()
                if not normalized_value:
                    continue
                if normalized_value.isdigit():
                    day = int(normalized_value)
                elif normalized_value in weekday_aliases:
                    day = weekday_aliases[normalized_value]
                else:
                    raise ValueError(f"Invalid day of week: {raw_value}")
            if day < 0 or day > 6:
                raise ValueError(f"Invalid day of week: {raw_value}")
            if day in seen:
                continue
            seen.add(day)
            normalized.append(day)
        if not normalized:
            raise ValueError("days_of_week is required")
        return normalized


class ProjectAutomationExecutionRead(BaseModel):
    execution_id: str
    automation_id: str
    scheduled_for: datetime
    window_start_at: datetime
    window_end_at: datetime
    status: str
    dedupe_key: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    discord_message_id: str | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class ProjectAutomationRead(BaseModel):
    automation_id: str
    tenant_id: str
    project_id: str
    kind: str
    enabled: bool
    timezone: str
    days_of_week: list[int] = Field(default_factory=list)
    local_time: str
    fallback_lookback_hours: int
    last_successful_window_end_at: datetime | None = None
    next_run_at: datetime
    executions: list[ProjectAutomationExecutionRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ProjectAutomationsWrite(BaseModel):
    automations: list[ProjectAutomationWrite] = Field(default_factory=list)


class ProjectAutomationsRead(BaseModel):
    automations: list[ProjectAutomationRead] = Field(default_factory=list)


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
    runtime_kind: str = "codex_cli"
    profile_name: str | None = None
    models: list[CodexModelOptionRead] = Field(default_factory=list)
    reasoning_efforts: list[CodexReasoningOptionRead] = Field(default_factory=list)


class GitHubInstallStart(BaseModel):
    install_url: str
    expires_at: datetime


class DiscordInstallStart(BaseModel):
    install_url: str
    expires_at: datetime


class AtlassianConnectStart(BaseModel):
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


class AgentRuntimeRoutingUpdate(BaseModel):
    role_routing: dict[str, str] = Field(default_factory=dict)
    name_routing: dict[str, str] = Field(default_factory=dict)
    selector_routing: dict[str, str] = Field(default_factory=dict)


class AgentExecutionProfileRead(BaseModel):
    profile_name: str
    runtime_kind: str
    cli_command: str
    model: str
    reasoning_effort: str | None = None
    tool_bridge_allowed: bool
    fallback_profile: str | None = None
    base_url: str | None = None
    api_key_secret_ref: str | None = None
    is_builtin: bool = False
    is_overridden: bool = False
    can_delete: bool = False
    can_reset: bool = False
    usage_references: list[str] = Field(default_factory=list)


class AgentExecutionProfileWrite(BaseModel):
    runtime_kind: str
    cli_command: str = ""
    model: str = Field(min_length=1)
    reasoning_effort: str | None = Field(default=None, pattern="^(low|medium|high)$")
    tool_bridge_allowed: bool = False
    fallback_profile: str | None = None
    base_url: str | None = None
    api_key_secret_ref: str | None = None


class AgentExecutionProfileCreate(AgentExecutionProfileWrite):
    profile_name: str = Field(min_length=1)


class AgentExecutionProfilesRead(BaseModel):
    profiles: dict[str, AgentExecutionProfileRead] = Field(default_factory=dict)


class AgentRuntimeRoutingDefaultsRead(BaseModel):
    role_routing: dict[str, str] = Field(default_factory=dict)
    name_routing: dict[str, str] = Field(default_factory=dict)
    selector_routing: dict[str, str] = Field(default_factory=dict)


class AgentRuntimeRoutingRead(BaseModel):
    role_routing: dict[str, str] = Field(default_factory=dict)
    name_routing: dict[str, str] = Field(default_factory=dict)
    selector_routing: dict[str, str] = Field(default_factory=dict)
    available_roles: list[str] = Field(default_factory=list)
    available_named_agents: list[str] = Field(default_factory=list)
    available_selectors: list[str] = Field(default_factory=list)
    available_profiles: dict[str, AgentExecutionProfileRead] = Field(default_factory=dict)
    effective_defaults: AgentRuntimeRoutingDefaultsRead = Field(default_factory=AgentRuntimeRoutingDefaultsRead)


class AgentRuntimeToolRead(BaseModel):
    tool_name: str
    category: str
    description: str
    stages: list[str] = Field(default_factory=list)


class AgentRuntimeToolsRead(BaseModel):
    available_stages: list[str] = Field(default_factory=list)
    tools: list[AgentRuntimeToolRead] = Field(default_factory=list)


class RunRead(BaseModel):
    run_id: str
    workflow_id: str
    attempt_number: int
    parent_run_id: str | None = None
    entry_mode: str
    entry_stage: str | None = None
    entry_checkpoint_id: str | None = None
    tenant_id: str
    project_id: str | None
    issue_key: str
    issue_summary: str | None = None
    issue_url: str | None = None
    repo_url: str | None
    branch: str | None
    pr_url: str | None
    status: str
    waiting_for_input: bool = False
    pending_input_request_id: str | None = None
    last_error: str | None
    plan: dict | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class WorkflowOperationAttemptRead(BaseModel):
    attempt_id: str
    attempt_number: int
    status: str
    error_category: str | None = None
    error_message: str | None = None
    status_detail: str | None = None
    retryable: bool = False
    next_retry_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    work_units: list["WorkflowOperationWorkUnitRead"] = Field(default_factory=list)


class WorkflowOperationWorkUnitAttemptRead(BaseModel):
    work_unit_attempt_id: str
    attempt_number: int
    operation_attempt_id: str
    status: str
    error_category: str | None = None
    error_message: str | None = None
    next_retry_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class WorkflowOperationWorkUnitRead(BaseModel):
    work_unit_id: str
    unit_key: str
    unit_kind: str
    idempotency_key: str
    input_fingerprint: str
    status: str
    error_category: str | None = None
    error_message: str | None = None
    completed_at: datetime | None = None
    attempts: list[WorkflowOperationWorkUnitAttemptRead] = Field(default_factory=list)


class WorkflowObservabilityEventRead(BaseModel):
    event_id: str
    event_sequence: int | None = None
    source: Literal["audit", "telemetry"]
    level: str
    event_kind: str
    message: str
    source_component: str | None = None
    run_id: str | None = None
    operation_id: str | None = None
    attempt_id: str | None = None
    agent_id: str | None = None
    invocation_id: str | None = None
    stage: str | None = None
    attempt: int | None = None
    stream: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    recorded_at: datetime


class WorkflowTranscriptEntryRead(BaseModel):
    entry_id: str
    recorded_at: datetime
    level: str
    title: str
    message: str
    source_component: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class WorkflowTranscriptSectionRead(BaseModel):
    kind: Literal["summary", "runtime", "prompts", "tool_calls", "external_requests", "external_responses", "outcome"]
    label: str
    entries: list[WorkflowTranscriptEntryRead] = Field(default_factory=list)


class WorkflowStepAttemptTranscriptRead(BaseModel):
    attempt_id: str
    attempt_number: int
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    error_category: str | None = None
    failure_message: str | None = None
    status_detail: str | None = None
    recommended_next_action: str | None = None
    sections: list[WorkflowTranscriptSectionRead] = Field(default_factory=list)


class WorkflowStepTranscriptRead(BaseModel):
    execution_id: str
    operation_id: str
    operation_label: str
    current_status: str
    source: Literal["audit", "telemetry"]
    attempts: list[WorkflowStepAttemptTranscriptRead] = Field(default_factory=list)


class AuditEventExportRequest(BaseModel):
    tenant_id: str
    project_id: str | None = None
    execution_id: str | None = None
    operation_id: str | None = None
    run_id: str | None = None
    issue_key: str | None = None
    recorded_after: datetime | None = None
    recorded_before: datetime | None = None


class WorkflowOperationRead(BaseModel):
    operation_id: str
    run_id: str | None = None
    operation_type: str
    status: str
    label: str | None = None
    description: str | None = None
    required: bool = True
    kind: str = "business"
    after: list[str] = Field(default_factory=list)
    supports: list[str] = Field(default_factory=list)
    definition_only: bool = False
    target_system: str | None = None
    target_ref: str | None = None
    summary: str | None = None
    can_retry: bool = False
    retry_unavailable_reason: str | None = None
    can_restart: bool = False
    restart_unavailable_reason: str | None = None
    attempts: list[WorkflowOperationAttemptRead] = Field(default_factory=list)
    events: list[WorkflowObservabilityEventRead] = Field(default_factory=list)


class WorkflowRetryPolicyRead(BaseModel):
    manual_retry_enabled: bool = True
    max_attempts: int = Field(default=1, ge=1)
    initial_interval_seconds: int = Field(default=0, ge=0)
    max_interval_seconds: int = Field(default=0, ge=0)
    backoff_coefficient: float = Field(default=1.0, ge=1.0)


class WorkflowTypeOperationRead(BaseModel):
    operation_type: str
    label: str
    description: str | None = None
    completion_required: bool = True
    kind: str = "business"
    after: list[str] = Field(default_factory=list)
    supports: list[str] = Field(default_factory=list)
    required: bool = True
    retryable: bool = False
    graph_index: int = 0
    status: str | None = None


class WorkflowTypeLifecycleStateRead(BaseModel):
    key: str
    label: str
    terminal: bool = False
    waits_for_input: bool = False


class WorkflowTypeLifecycleTransitionRead(BaseModel):
    from_state: str = Field(alias="from")
    to_state: str
    label: str

    model_config = ConfigDict(populate_by_name=True)


class WorkflowTypeLifecycleRead(BaseModel):
    state_path_kind: str = "operation"
    execution_modes: list[str] = Field(default_factory=list)
    conditional_paths: list[str] = Field(default_factory=list)
    states: list[WorkflowTypeLifecycleStateRead] = Field(default_factory=list)
    transitions: list[WorkflowTypeLifecycleTransitionRead] = Field(default_factory=list)


class WorkflowTypeRead(BaseModel):
    key: str
    label: str
    description: str | None = None
    orchestration_backend: Literal["legacy", "temporal", "database"]
    retry_policy: WorkflowRetryPolicyRead = Field(default_factory=WorkflowRetryPolicyRead)
    capabilities: dict[str, object] = Field(default_factory=dict)
    lifecycle: WorkflowTypeLifecycleRead = Field(default_factory=WorkflowTypeLifecycleRead)
    operations: list[WorkflowTypeOperationRead] = Field(default_factory=list)


class WorkflowExecutionPreviewRead(BaseModel):
    execution_id: str
    workflow_id: str
    source_system: str
    source_ref: str
    display_name: str | None = None
    status: str
    waiting_on: str | None = None
    next_step: str | None = None
    failure_reason: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


class WorkflowTypeSummaryRead(BaseModel):
    key: str
    label: str
    description: str | None = None
    operation_count: int = 0
    execution_count: int = 0
    latest_execution_at: datetime | None = None


class WorkflowTypeDetailRead(BaseModel):
    key: str
    label: str
    description: str | None = None
    orchestration_backend: Literal["legacy", "temporal", "database"]
    retry_policy: WorkflowRetryPolicyRead = Field(default_factory=WorkflowRetryPolicyRead)
    capabilities: dict[str, object] = Field(default_factory=dict)
    lifecycle: WorkflowTypeLifecycleRead = Field(default_factory=WorkflowTypeLifecycleRead)
    operations: list[WorkflowTypeOperationRead] = Field(default_factory=list)
    execution_count: int = 0
    latest_execution_at: datetime | None = None
    recent_executions: list[WorkflowExecutionPreviewRead] = Field(default_factory=list)


class WorkflowStatePathEntryRead(BaseModel):
    key: str
    label: str
    status: str
    recorded_at: datetime | None = None
    detail: str | None = None


class WorkflowLinkRead(BaseModel):
    kind: str
    label: str
    ref: str | None = None
    url: str | None = None
    status: str | None = None


class WorkflowRead(BaseModel):
    execution_id: str
    workflow_id: str
    tenant_id: str
    project_id: str | None
    source_system: str
    source_ref: str
    display_name: str | None = None
    repo_url: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    orchestration_backend: Literal["legacy", "temporal", "database"]
    dedupe_scope: str
    status: str
    workflow_type: WorkflowTypeRead
    current_state: str
    waiting_on: str | None = None
    next_step: str | None = None
    active_run_id: str | None = None
    latest_checkpoint_id: str | None = None
    source_workflow_id: str | None = None
    source_run_id: str | None = None
    failure_reason: str | None = None
    pending_input_request_id: str | None = None
    latest_checkpoint_kind: str | None = None
    state_path: list[WorkflowStatePathEntryRead] = Field(default_factory=list)
    completed_steps: list[str] = Field(default_factory=list)
    failed_steps: list[str] = Field(default_factory=list)
    pending_steps: list[str] = Field(default_factory=list)
    retrying_steps: list[str] = Field(default_factory=list)
    conditional_branches_taken: list[str] = Field(default_factory=list)
    conditional_branches_available: list[str] = Field(default_factory=list)
    can_resume: bool = False
    resume_unavailable_reason: str | None = None
    links: list[WorkflowLinkRead] = Field(default_factory=list)
    operations: list[WorkflowOperationRead] = Field(default_factory=list)
    runs: list[RunRead] = Field(default_factory=list)
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class WorkflowOperationRetryRead(BaseModel):
    workflow: WorkflowRead
    started_attempt: WorkflowOperationAttemptRead | None = None


class WorkflowExecutionStartRequest(BaseModel):
    tenant_id: str
    project_id: str | None = None
    input: dict[str, object] = Field(default_factory=dict)


class WorkflowExecutionStartRead(BaseModel):
    execution_id: str
    workflow_id: str
    workflow_type_key: str
    status: str
    started_attempt_id: str | None = None


class StartWorkIssueRead(BaseModel):
    issue_key: str
    run_id: str | None = None
    status: str
    reason: str | None = None


class WorkflowStartWorkRead(BaseModel):
    workflow: WorkflowRead
    queued: list[StartWorkIssueRead] = Field(default_factory=list)
    skipped: list[StartWorkIssueRead] = Field(default_factory=list)
    promoted_issue_keys: list[str] = Field(default_factory=list)
    started_attempt: WorkflowOperationAttemptRead | None = None


class WorkflowStartWorkRequest(BaseModel):
    action_token: str | None = None


class StartEngineeringPreviewRead(BaseModel):
    tenant_id: str
    project_id: str
    execution_id: str
    issue_key: str
    display_name: str | None = None
    workflow_status: str
    can_start: bool
    unavailable_reason: str | None = None


class WorkflowOperationRestartRequest(BaseModel):
    restart_reason: str = Field(default="Restarted stale running workflow operation attempt.")


class WorkflowAttemptCreateRequest(BaseModel):
    mode: str = Field(pattern="^(fresh|restart|resume)$")
    checkpoint_kind: str | None = Field(default=None, pattern="^(pm|execution)$")

    @model_validator(mode="after")
    def validate_checkpoint_contract(self) -> WorkflowAttemptCreateRequest:
        if self.mode == "fresh":
            if self.checkpoint_kind is not None:
                raise ValueError("checkpoint_kind must be omitted for fresh attempts")
            return self
        if self.checkpoint_kind is None:
            raise ValueError("checkpoint_kind is required for restart and resume attempts")
        return self


class RunEventRead(BaseModel):
    event_type: str
    run_id: str
    issue_key: str | None = None
    project_id: str | None = None
    agent_id: str
    recorded_at: datetime


class LoggingPaneEventRead(BaseModel):
    event_id: str
    event_sequence: int | None = None
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


class AdminNotificationRead(BaseModel):
    notification_id: str
    tenant_id: str | None = None
    project_id: str | None = None
    scope_type: str
    scope_id: str | None = None
    source: str
    kind: str
    severity: str
    title: str
    detail: str
    action_label: str | None = None
    action_path: str | None = None
    fingerprint: str
    status: str
    context: dict[str, object] = Field(default_factory=dict)
    first_emitted_at: datetime
    last_emitted_at: datetime
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None


class AdminNotificationListRead(BaseModel):
    notifications: list[AdminNotificationRead] = Field(default_factory=list)


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


class PlatformServiceInstanceRead(BaseModel):
    instance_id: str
    label: str
    status: str
    summary: str
    updated_at: datetime | None = None
    capabilities: list[str] = Field(default_factory=list)
    active_run_count: int = 0
    runtime_dependencies: dict[str, dict[str, object]] = Field(default_factory=dict)


class WorkerRuntimeAuthRequestRead(BaseModel):
    request_id: str
    service_instance_id: str
    runtime_kind: str
    status: str
    remediation_text: str | None = None
    requested_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    expires_at: datetime | None = None
    last_error: str | None = None


class PlatformServiceStatusRead(BaseModel):
    service_id: str
    label: str
    status: str
    summary: str
    updated_at: datetime | None = None
    capabilities: list[str] = Field(default_factory=list)
    runtime_dependencies: dict[str, dict[str, object]] = Field(default_factory=dict)
    instances: list[PlatformServiceInstanceRead] = Field(default_factory=list)


class PlatformStatusRead(BaseModel):
    services: list[PlatformServiceStatusRead] = Field(default_factory=list)


class WebhookQueueJobRead(BaseModel):
    job_id: str
    transport: str
    tenant_id: str | None = None
    project_id: str | None = None
    subject_key: str
    related_run_id: str | None = None
    dedupe_key: str | None = None
    request_id: str
    event_type: str | None = None
    status: str
    lease_expires_at: datetime | None = None
    available_at: datetime
    attempt_count: int
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class WebhookQueueSummaryRead(BaseModel):
    pending_count: int
    processing_count: int
    failed_count: int
    done_count: int


class WebhookQueueJobPageRead(BaseModel):
    items: list[WebhookQueueJobRead] = Field(default_factory=list)
    total: int
    limit: int
    offset: int
    summary: WebhookQueueSummaryRead


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
