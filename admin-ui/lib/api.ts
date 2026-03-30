import { DEFAULT_API_BASE_URL } from "@/lib/auth-constants";

export type JiraConfig = {
  connection_id: string | null;
  project_keys: string[];
  ready_statuses: string[];
  ready_jql: string | null;
  ready_label: string;
  in_progress_label: string;
  blocked_label: string;
  done_label: string | null;
  webhook_secret_ref: string | null;
};

export type GithubConfig = {
  webhook_secret_ref: string | null;
  installation_id: string | null;
};

export type ReposConfig = {
  github_repository: string | null;
};

export type PolicyConfig = {
  allow_jira_transitions: boolean;
  allow_pr_creation: boolean;
  allow_code_reviews: boolean;
  allow_pr_remediation: boolean;
  allow_manual_pr_fix_requests: boolean;
  allow_label_mutations: boolean;
  allow_auto_merge: boolean;
  max_runtime_minutes: number;
  max_dev_test_review_loops: number;
  max_pr_auto_remediation_loops: number;
  max_concurrent_runs: number;
  allowed_commands: string[];
  require_agents_md: boolean;
  knowledge_base_enabled: boolean;
  knowledge_auto_answer_mode: "safe" | "balanced" | "aggressive";
  codex_model?: string | null;
  codex_reasoning_effort?: "low" | "medium" | "high" | null;
};

export type DiscordConfig = {
  guild_id?: string | null;
  installed_at?: string | null;
  installer_user_id?: string | null;
  channel_id?: string | null;
  onboarding_channel_id?: string | null;
  onboarding_invite_expires_in_seconds?: number | null;
  onboarding_invite_max_uses?: number | null;
  notify_events: string[];
  allowed_user_ids?: string[];
  command_secret_ref?: string | null;
  live_voice_enabled?: boolean;
  live_voice_room_links?: Record<string, string>;
  live_voice_channel_id?: string | null;
  live_voice_linked_text_channel_id?: string | null;
};

export type TenantCreatePayload = {
  name: string;
  is_enabled: boolean;
  jira: JiraConfig;
  github: GithubConfig;
  repos: ReposConfig;
  policy: PolicyConfig;
  discord: DiscordConfig | null;
  experience?: Record<string, unknown>;
};

export type TenantUpdatePayload = {
  name: string;
  is_enabled: boolean;
  jira: JiraConfig;
  github: GithubConfig;
  repos: ReposConfig;
  policy: PolicyConfig;
  discord: DiscordConfig | null;
  experience?: Record<string, unknown>;
};

export type TenantRecord = {
  tenant_id: string;
  name: string;
  is_enabled: boolean;
  archived_at?: string | null;
  purge_after_at?: string | null;
  jira: JiraConfig;
  github: GithubConfig;
  repos: ReposConfig;
  policy: PolicyConfig;
  discord: DiscordConfig | null;
  experience: Record<string, unknown>;
  setup_state: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type GitHubRepositoryRecord = {
  full_name: string;
  html_url: string;
  default_branch: string;
  private: boolean;
};

export type JiraProjectRecord = {
  key: string;
  name: string;
};

export type ProjectPolicyOverrides = Partial<
  Pick<
    PolicyConfig,
    | "allow_jira_transitions"
    | "allow_pr_creation"
    | "allow_code_reviews"
    | "allow_pr_remediation"
    | "allow_manual_pr_fix_requests"
    | "allow_label_mutations"
    | "allow_auto_merge"
    | "max_dev_test_review_loops"
    | "max_pr_auto_remediation_loops"
    | "max_concurrent_runs"
    | "allowed_commands"
    | "require_agents_md"
    | "knowledge_base_enabled"
    | "knowledge_auto_answer_mode"
    | "codex_model"
    | "codex_reasoning_effort"
  >
>;

export type CodexModelOptionRecord = {
  id: string;
  label: string;
  description?: string | null;
};

export type CodexModelCatalogRecord = {
  default_model: string;
  default_reasoning_effort: "low" | "medium" | "high";
  runtime_kind: string;
  profile_name?: string | null;
  models: CodexModelOptionRecord[];
  reasoning_efforts: CodexModelOptionRecord[];
};

export type ProjectDiscordConfig = {
  channel_id?: string | null;
  notify_events?: string[];
  ask_thread_channel_ids?: string[];
  seed_followup_thread_channel_ids?: string[];
  live_voice_enabled?: boolean;
  live_voice_room_links?: Record<string, string>;
  live_voice_channel_id?: string | null;
  live_voice_linked_text_channel_id?: string | null;
};

export type ProjectRecord = {
  project_id: string;
  tenant_id: string;
  name: string;
  github_repository: string;
  jira_project_key: string;
  policy_overrides: ProjectPolicyOverrides;
  environment: Record<string, string>;
  secret_refs: Record<string, string>;
  discord: ProjectDiscordConfig | null;
  effective_policy: PolicyConfig;
  is_archived: boolean;
  created_at: string;
  updated_at: string;
};

export type ProjectAutomationExecutionRecord = {
  execution_id: string;
  automation_id: string;
  scheduled_for: string;
  window_start_at: string;
  window_end_at: string;
  status: string;
  dedupe_key: string;
  started_at: string | null;
  completed_at: string | null;
  discord_message_id: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
};

export type ProjectAutomationRecord = {
  automation_id: string;
  project_id: string;
  tenant_id: string;
  kind: string;
  enabled: boolean;
  timezone: string;
  days_of_week: number[];
  local_time: string;
  fallback_lookback_hours: number;
  last_successful_window_end_at: string | null;
  next_run_at: string;
  executions: ProjectAutomationExecutionRecord[];
  created_at: string;
  updated_at: string;
};

export type ProjectAutomationWritePayload = {
  kind: string;
  enabled: boolean;
  timezone: string;
  days_of_week: Array<number | string>;
  local_time: string;
  fallback_lookback_hours: number;
};

export type ProjectAutomationsPayload = {
  automations: ProjectAutomationWritePayload[];
};

export type ProjectAutomationsRecord = {
  automations: ProjectAutomationRecord[];
};

export type ProjectCreatePayload = {
  name: string;
  github_repository: string;
  jira_project_key: string;
  policy_overrides?: ProjectPolicyOverrides;
  environment?: Record<string, string>;
  secret_refs?: Record<string, string>;
  discord?: ProjectDiscordConfig | null;
};

export type ProjectUpdatePayload = {
  name: string;
  github_repository: string;
  jira_project_key: string;
  policy_overrides?: ProjectPolicyOverrides;
  environment?: Record<string, string>;
  secret_refs?: Record<string, string>;
  discord?: ProjectDiscordConfig | null;
  is_archived: boolean;
};

export type ProjectKnowledgeAssetRecord = {
  asset_id: string;
  tenant_id: string;
  project_id: string;
  title: string;
  mime_type: string | null;
  source_type: string;
  source_ref: string | null;
  source_timestamp: string | null;
  chunk_count: number;
  status: string;
  updated_at: string;
  created_at: string;
};

export type ProjectKnowledgeSourceRecord = {
  source_id: string;
  tenant_id: string;
  project_id: string;
  connector_type: string;
  display_name: string;
  status: "active" | "disabled";
  sync_mode: "manual" | "scheduled";
  config_json: Record<string, unknown>;
  config_summary: string;
  supports_sync_now: boolean;
  supports_scheduled_sync: boolean;
  last_synced_at: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
};

export type ProjectKnowledgeAssetDetailRecord = ProjectKnowledgeAssetRecord & {
  text_content: string | null;
  metadata_json: Record<string, unknown>;
  facts: ProjectKnowledgeFactRecord[];
};

export type ProjectKnowledgeChunkRecord = {
  chunk_id: string;
  asset_id: string;
  tenant_id: string;
  project_id: string;
  chunk_index: number;
  content: string;
  token_count: number;
  source_timestamp: string | null;
  created_at: string;
  updated_at: string;
};

export type ProjectKnowledgeFactRecord = {
  fact_id: string;
  asset_id: string;
  chunk_id: string | null;
  tenant_id: string;
  project_id: string;
  fact_type: string;
  fact_key: string;
  fact_value: string;
  approval_state: string;
  confidence: number;
  is_inferred: boolean;
  metadata_json: Record<string, unknown>;
  source_timestamp: string | null;
  superseded_at: string | null;
  created_at: string;
  updated_at: string;
};

export type ProjectKnowledgeAssetPageRecord = {
  items: ProjectKnowledgeAssetRecord[];
  total: number;
  limit: number;
  offset: number;
};

export type ProjectKnowledgeSourcePageRecord = {
  items: ProjectKnowledgeSourceRecord[];
  total: number;
};

export type ProjectKnowledgeChunkPageRecord = {
  items: ProjectKnowledgeChunkRecord[];
  total: number;
  limit: number;
  offset: number;
};

export type ProjectKnowledgeDebugMatchRecord = {
  layer: string;
  score: number;
  asset_id: string;
  source_type: string;
  title: string;
  source_ref: string | null;
  source_timestamp: string | null;
  fact_id: string | null;
  chunk_id: string | null;
  snippet: string;
  metadata: Record<string, unknown>;
};

export type ProjectKnowledgeDebugSearchRecord = {
  query: string;
  items: ProjectKnowledgeDebugMatchRecord[];
};

export type ProjectKnowledgeStatsRecord = {
  total_assets: number;
  total_chunks: number;
  total_facts: number;
  approved_facts: number;
  pending_review_facts: number;
  superseded_facts: number;
  ready_assets: number;
  pending_review_assets: number;
  rejected_assets: number;
  latest_asset_updated_at: string | null;
  source_type_counts: Record<string, number>;
};

export type ProjectKnowledgeAssetCreatePayload = {
  title: string;
  mime_type?: string | null;
  source_type: string;
  source_ref?: string;
  source_timestamp?: string;
  text_content?: string;
  content_base64?: string;
};

export type ProjectKnowledgeAssetStatusUpdatePayload = {
  status: "pending_review" | "ready" | "rejected";
};

export type ProjectKnowledgeSourceCreatePayload = {
  connector_type: "jira" | "google_drive" | "discord";
  display_name?: string | null;
  status?: "active" | "disabled";
  sync_mode?: "manual" | "scheduled";
  config_json?: Record<string, unknown>;
};

export type ProjectKnowledgeSourceUpdatePayload = {
  display_name?: string | null;
  status?: "active" | "disabled";
  sync_mode?: "manual" | "scheduled";
  config_json?: Record<string, unknown>;
};

export type ProjectKnowledgeSyncResult = {
  ok: boolean;
  synced_assets: number;
  skipped_assets: number;
  created_assets: number;
  updated_assets: number;
  unchanged_assets: number;
  deleted_assets: number;
  failed_assets: number;
  details: string | null;
};

export type KnowledgeJiraSyncProjectStatusRecord = {
  tenant_id: string;
  project_id: string;
  jira_project_key: string;
  state: string;
  failure_category: string | null;
  last_error: string | null;
  last_attempted_at: string | null;
  last_successful_sync_at: string | null;
  next_retry_at: string | null;
  consecutive_failures: number;
};

export type KnowledgeJiraSyncRuntimeRecord = {
  state: string;
  enabled: boolean;
  database_backend: string;
  started_at: string | null;
  stopped_at: string | null;
  last_pass_started_at: string | null;
  last_pass_finished_at: string | null;
  last_heartbeat_at: string | null;
  leader_acquired: boolean;
  service_instance_id: string | null;
  stale: boolean;
  projects: KnowledgeJiraSyncProjectStatusRecord[];
};

export type PlatformServiceStatusRecord = {
  service_id: string;
  label: string;
  status: "healthy" | "degraded" | "idle" | "unavailable" | string;
  summary: string;
  updated_at: string | null;
  capabilities: string[];
  instances?: PlatformServiceInstanceRecord[];
};

export type PlatformServiceInstanceRecord = {
  instance_id: string;
  label: string;
  status: "healthy" | "degraded" | "idle" | "unavailable" | "busy" | "stale" | "stopped" | string;
  summary?: string | null;
  last_heartbeat_at?: string | null;
  updated_at: string | null;
  capabilities: string[];
  current_run_id?: string | null;
  active_run_count?: number;
};

export type PlatformStatusRecord = {
  services: PlatformServiceStatusRecord[];
};

export type JiraWebhookActionResult = {
  ok: boolean;
  action: string;
  details: string;
  webhook_ids: number[];
};

export type JiraWebhookDiagnosticsRecord = {
  tenant_id: string;
  connected: boolean;
  webhook_url: string;
  managed_webhook_ids: number[];
  last_provisioned_at: string | null;
  last_received_at: string | null;
  last_delivery_id: string | null;
  last_issue_key: string | null;
  last_error: string | null;
  recent_delivery_window_minutes: number;
  recent_delivery_ok: boolean;
};

export type ReadyIssuePreviewRecord = {
  key: string;
  summary: string;
  status: string;
};

export type ReadyGatePreviewRecord = {
  ready_statuses: string[];
  ready_jql: string;
  eligible_issues: ReadyIssuePreviewRecord[];
  guidance: string;
};

export const RUN_STATUSES = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "blocked",
  "cancelled",
] as const;

export type RunStatus = (typeof RUN_STATUSES)[number];

export type RunRecord = {
  run_id: string;
  tenant_id: string;
  project_id: string | null;
  issue_key: string;
  issue_summary: string | null;
  issue_url: string | null;
  repo_url: string | null;
  branch: string | null;
  pr_url: string | null;
  dev_session_id: string | null;
  pm_session_id: string | null;
  orchestrated_session_id: string | null;
  status: RunStatus;
  last_error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  plan: Record<string, unknown> | null;
};

export type RunRerunPayload = {
  mode: "fresh" | "resume";
  resume_stage?: "orchestrated" | "pm" | "dev" | "review";
};

export type RunEventRecord = {
  event_type: string;
  run_id: string;
  issue_key: string | null;
  project_id: string | null;
  agent_id: string;
  recorded_at: string;
};

export type RunLogEventRecord = {
  run_id: string;
  issue_key: string | null;
  project_id: string | null;
  agent_id: string;
  invocation_id?: string | null;
  channel?: string | null;
  command?: string | null;
  working_dir: string | null;
  stage: string;
  attempt: number | null;
  stream: string;
  message: string;
  recorded_at: string;
};

export type TokenTimelineTurnRecord = {
  turn_id: string;
  invocation_id: string;
  stage: string;
  attempt: number | null;
  recorded_at: string;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  delta_input: number;
  delta_uncached: number;
  delta_output: number;
  runtime_ms: number | null;
  is_growth_spike: boolean;
  spike_reason: string[];
};

export type TokenTimelineTotals = {
  input: number;
  uncached_input: number;
  output: number;
  cached_input: number;
  cache_ratio: number;
  total_io: number;
  avg_runtime_ms: number;
  p95_runtime_ms: number;
};

export type TokenTimelineRecord = {
  run_id: string;
  issue_key: string;
  model: string | null;
  status: string;
  totals: TokenTimelineTotals;
  turns: TokenTimelineTurnRecord[];
};

export type TokenAlertRecord = {
  rule: string;
  severity: string;
  run_id: string;
  turn_id: string | null;
  stage: string | null;
  attempt: number | null;
  value: number | null;
  threshold: number | null;
  message: string;
};

export type TokenOverviewSeriesPoint = {
  day: string;
  total_input: number;
  total_uncached_input: number;
  total_output: number;
  total_io: number;
  delta_input: number;
  delta_uncached: number;
  delta_output: number;
  delta_total_io: number;
  avg_runtime_ms: number;
  p95_runtime_ms: number;
  run_count: number;
};

export type TokenOverviewKpi = {
  total_input: number;
  total_uncached_input: number;
  total_output: number;
  total_io: number;
  cache_ratio: number;
  avg_runtime_ms: number;
  p95_runtime_ms: number;
  avg_io_per_run: number;
  p95_io_per_run: number;
  retest_waste_score: number;
};

export type TokenOverviewTopRun = {
  run_id: string;
  issue_key: string;
  tenant_id: string;
  project_id: string | null;
  status: string;
  input: number;
  uncached_input: number;
  output: number;
  total_io: number;
  delta_total_io: number;
  cache_ratio: number;
};

export type TokenOverviewRecord = {
  kpis: TokenOverviewKpi;
  series_by_day: TokenOverviewSeriesPoint[];
  top_costly_runs: TokenOverviewTopRun[];
  alerts: TokenAlertRecord[];
};

export type TokenCompareStageTotals = {
  stage: string;
  input: number;
  uncached_input: number;
  output: number;
  total_io: number;
};

export type TokenCompareRunTotals = {
  input: number;
  uncached_input: number;
  output: number;
  cached_input: number;
  total_io: number;
  cache_ratio: number;
};

export type TokenCompareRun = {
  run_id: string;
  issue_key: string;
  status: string;
  totals: TokenCompareRunTotals;
  stage_totals: TokenCompareStageTotals[];
};

export type TokenCompareWaterfallStep = {
  run_id: string;
  turn_order: number;
  turn_id: string;
  recorded_at: string;
  stage: string;
  attempt: number | null;
  delta_input: number;
  uncached_delta: number;
  output_tokens: number;
  delta_reason: string[];
};

export type TokenCompareRecord = {
  runs: TokenCompareRun[];
  align_axis: number[];
  waterfall: TokenCompareWaterfallStep[];
};

export type TokenCompareRequestBody = {
  run_ids: string[];
  align_by?: "turn_sequence" | "recorded_at";
};

export type TokenStageDiagnostic = {
  stage: string;
  avg_delta: number;
  avg_uncached_delta: number;
  retry_impact_index: number;
  run_count: number;
};

export type TokenStageHeatmapCell = {
  stage: string;
  attempt: number;
  avg_delta: number;
  avg_uncached_delta: number;
  sample_count: number;
};

export type TokenHeavyCommand = {
  command_signature: string;
  stage: string;
  spike_count: number;
  avg_delta: number;
  avg_uncached_delta: number;
};

export type TokenScatterPoint = {
  run_id: string;
  issue_key: string;
  stage: string;
  attempt: number | null;
  runtime_ms: number | null;
  token_delta: number;
  recorded_at: string;
};

export type TokenIssueStageUsage = {
  issue_key: string;
  stage: string;
  input: number;
  uncached_input: number;
  output: number;
  total_io: number;
  delta_total_io: number;
  run_count: number;
};

export type TokenStageDiagnosticsRecord = {
  stages: TokenStageDiagnostic[];
  heavy_commands: TokenHeavyCommand[];
  scatter_points: TokenScatterPoint[];
  heatmap: TokenStageHeatmapCell[];
  retest_waste_score: number;
  issue_stage_totals: TokenIssueStageUsage[];
};

export type TokenProjectStageDiagnostics = {
  project_id: string;
  project_name: string | null;
  stages: TokenStageDiagnostic[];
  retest_waste_score: number;
};

export type TokenStageDiagnosticsCompareRecord = {
  projects: TokenProjectStageDiagnostics[];
};

export type ManagedSecretRecord = {
  secret_ref: string;
  source: "managed" | "environment" | "missing" | string;
  updated_at: string | null;
};

export type ManagedSecretResolveResult = {
  secret_ref: string;
  source: "managed" | "environment" | "missing" | string;
  resolved: boolean;
};

export type AgentExecutionProfileRecord = {
  profile_name: string;
  runtime_kind: string;
  cli_command: string;
  model: string;
  reasoning_effort: "low" | "medium" | "high" | null;
  tool_bridge_allowed: boolean;
  fallback_profile: string | null;
  base_url: string | null;
  api_key_secret_ref: string | null;
  is_builtin: boolean;
  is_overridden: boolean;
  can_delete: boolean;
  can_reset: boolean;
  usage_references: string[];
};

export type AgentExecutionProfileWritePayload = {
  runtime_kind: string;
  cli_command: string;
  model: string;
  reasoning_effort: "low" | "medium" | "high" | null;
  tool_bridge_allowed: boolean;
  fallback_profile: string | null;
  base_url: string | null;
  api_key_secret_ref: string | null;
};

export type AgentExecutionProfileCreatePayload = AgentExecutionProfileWritePayload & {
  profile_name: string;
};

export type AgentExecutionProfilesRecord = {
  profiles: Record<string, AgentExecutionProfileRecord>;
};

export type AgentRuntimeToolRecord = {
  tool_name: string;
  category: string;
  description: string;
  stages: string[];
};

export type AgentRuntimeToolsRecord = {
  available_stages: string[];
  tools: AgentRuntimeToolRecord[];
};

export type AgentRuntimeRoutingDefaultsRecord = {
  role_routing: Record<string, string>;
  name_routing: Record<string, string>;
  selector_routing: Record<string, string>;
};

export type AgentRuntimeRoutingRecord = {
  role_routing: Record<string, string>;
  name_routing: Record<string, string>;
  selector_routing: Record<string, string>;
  available_roles: string[];
  available_named_agents: string[];
  available_selectors: string[];
  available_profiles: Record<string, AgentExecutionProfileRecord>;
  effective_defaults: AgentRuntimeRoutingDefaultsRecord;
};

export type DiscordAllowlistRequestRecord = {
  project_id: string | null;
  user_id: string;
  requested_at: string;
  channel_id: string | null;
  reason: string | null;
  permissions?: string[];
};

export type DiscordAllowlistApprovalResult = {
  ok: boolean;
  details: string;
  project_id: string | null;
  user_id: string;
  notified: boolean;
};

export type Credentials = {
  apiBaseUrl: string;
};

export type MembershipRecord = {
  membership_id: string;
  tenant_id: string;
  role: string;
  permission_keys: string[];
  effective_mode: "technical" | "non_technical";
  mode_override: "technical" | "non_technical" | null;
  onboarding_kind: "tenant_admin_setup" | "member_join";
  first_signed_in_at: string | null;
  onboarding_completed_at: string | null;
  onboarding_version: string | null;
  team_ids: string[];
  discord_state: Record<string, unknown>;
};

export type AuthenticatedPrincipalRecord = {
  principal_type: "platform_super_admin" | "tenant_user";
  username?: string | null;
  user_id?: string | null;
  email?: string | null;
  full_name?: string | null;
  memberships: MembershipRecord[];
};

export type AdminLoginInput = {
  apiBaseUrl: string;
  username: string;
  password: string;
};

export type RegistrationInput = {
  full_name: string;
  email: string;
  password: string;
  tenant_name: string;
};

export type PasswordResetRequestInput = {
  email: string;
};

export type PasswordResetConfirmInput = {
  token: string;
  new_password: string;
};

export type RegistrationResponse = {
  access_token: string;
  token_type: string;
  expires_in: number;
  principal: AuthenticatedPrincipalRecord;
  tenant: TenantRecord;
};

export type InviteAcceptInput = {
  token: string;
  password: string;
  full_name?: string | null;
};

export type DeliverySummaryRecord = {
  summary: {
    completed_count: number;
    in_review_count: number;
    blocked_count: number;
    failed_count: number;
    queued_count: number;
    median_cycle_time_hours: number | null;
    average_cycle_time_hours: number | null;
  };
  timeline: Array<{
    run_id: string;
    project_id: string | null;
    issue_key: string;
    issue_summary: string | null;
    status: string;
    completed_at: string | null;
    started_at: string | null;
    pr_url: string | null;
  }>;
};

export type TenantInviteRecord = {
  invite_id: string;
  tenant_id: string;
  email: string;
  full_name: string | null;
  role: string;
  team_ids: string[];
  mode_override: "technical" | "non_technical" | null;
  status: string;
  invite_url: string | null;
  expires_at: string;
  accepted_at: string | null;
  revoked_at: string | null;
  created_at: string;
  updated_at: string;
};

export type TenantTeamRecord = {
  team_id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  permission_keys: string[];
  created_at: string;
  updated_at: string;
};

export type TenantMemberRecord = {
  membership_id: string;
  tenant_id: string;
  user_id: string;
  email: string;
  full_name: string | null;
  is_active: boolean;
  role: string;
  permission_keys: string[];
  effective_mode: "technical" | "non_technical";
  mode_override: "technical" | "non_technical" | null;
  onboarding_kind: "tenant_admin_setup" | "member_join";
  first_signed_in_at: string | null;
  onboarding_completed_at: string | null;
  onboarding_version: string | null;
  team_ids: string[];
  discord_state: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type TenantMemberUpdatePayload = {
  role: "tenant_admin" | "technical_member" | "business_member";
  team_ids: string[];
  mode_override: "technical" | "non_technical" | null;
  is_active: boolean;
};

export type TenantTeamCreatePayload = {
  name: string;
  description?: string | null;
  permission_keys: string[];
};

export type TenantInviteCreatePayload = {
  email: string;
  full_name?: string | null;
  role: "tenant_admin" | "technical_member" | "business_member";
  team_ids: string[];
  mode_override: "technical" | "non_technical" | null;
};

export type TenantDiscordIdentityRecord = {
  oauth_configured?: boolean;
  linked: boolean;
  discord_user_id?: string | null;
  discord_username?: string | null;
  discord_global_name?: string | null;
  discord_avatar_hash?: string | null;
  linked_at?: string | null;
};

export type TenantDiscordInviteRecord = {
  invite_url: string;
  expires_at: string | null;
  max_uses: number | null;
};

export type DiscordInstallStartRecord = {
  install_url: string;
  expires_at: string;
};

export type TenantUserSettingsUpdatePayload = {
  mode_override: "technical" | "non_technical" | null;
};

export type TenantUserPasswordChangePayload = {
  current_password: string;
  new_password: string;
};

export type TenantUserProfileUpdatePayload = {
  full_name: string;
};

function parseResponseBody(text: string): unknown {
  if (!text) {
    return null;
  }

  try {
    return JSON.parse(text);
  } catch {
    return { detail: text };
  }
}

function stringifyErrorDetail(detail: unknown): string {
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === "string") {
          return item;
        }
        if (item && typeof item === "object") {
          const record = item as { msg?: unknown; loc?: unknown };
          const message = typeof record.msg === "string" ? record.msg : null;
          const location = Array.isArray(record.loc)
            ? record.loc
                .map((part) => String(part))
                .filter(Boolean)
                .join(".")
            : null;
          if (message && location) {
            return `${location}: ${message}`;
          }
          return message;
        }
        return null;
      })
      .filter((value): value is string => Boolean(value));
    if (messages.length > 0) {
      return messages.join("; ");
    }
  }
  if (detail && typeof detail === "object") {
    try {
      return JSON.stringify(detail);
    } catch {
      return "Unexpected error";
    }
  }
  return "Unexpected error";
}

async function request<T>(
  credentials: Credentials,
  path: string,
  init?: RequestInit
): Promise<T> {
  void credentials;
  const response = await fetch(`/api/bff${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {})
    }
  });

  const text = await response.text();
  const body = parseResponseBody(text);

  if (!response.ok) {
    const detail =
      typeof body === "object" && body && "detail" in body
        ? stringifyErrorDetail((body as { detail: unknown }).detail)
        : response.statusText;
    throw new Error(`${response.status}: ${detail}`);
  }

  return body as T;
}

export async function verifyAdminCredentials(credentials: Credentials): Promise<void> {
  await request<{ username: string }>(credentials, "/api/admin/auth/me");
}

export async function readAuthenticatedPrincipal(credentials: Credentials): Promise<AuthenticatedPrincipalRecord> {
  return request<AuthenticatedPrincipalRecord>(credentials, "/api/app/auth/me");
}

export async function authenticateAdmin(input: AdminLoginInput): Promise<Credentials> {
  const base = input.apiBaseUrl.replace(/\/$/, "");
  const response = await fetch(`${base}/api/admin/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: input.username,
      password: input.password
    })
  });

  const text = await response.text();
  const body = parseResponseBody(text);
  if (!response.ok) {
    const detail =
      typeof body === "object" && body && "detail" in body
        ? stringifyErrorDetail((body as { detail: unknown }).detail)
        : response.statusText;
    throw new Error(`${response.status}: ${detail}`);
  }

  const parsed = body as { access_token?: string };
  if (!parsed.access_token) {
    throw new Error("Authentication failed: missing access token");
  }

  void parsed.access_token;
  return {
    apiBaseUrl: base
  };
}

export async function registerTenantAdministrator(
  input: RegistrationInput,
  apiBaseUrl = ""
): Promise<RegistrationResponse> {
  const base = apiBaseUrl.replace(/\/$/, "");
  const response = await fetch(`${base}/api/public/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input)
  });

  const text = await response.text();
  const body = parseResponseBody(text);
  if (!response.ok) {
    const detail =
      typeof body === "object" && body && "detail" in body
        ? stringifyErrorDetail((body as { detail: unknown }).detail)
        : response.statusText;
    throw new Error(`${response.status}: ${detail}`);
  }

  return body as RegistrationResponse;
}

export async function acceptPublicInvite(
  input: InviteAcceptInput,
  apiBaseUrl = ""
): Promise<{
  access_token: string;
  token_type: string;
  expires_in: number;
  principal: AuthenticatedPrincipalRecord;
}> {
  const base = apiBaseUrl.replace(/\/$/, "");
  const response = await fetch(`${base}/api/public/invites/accept`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input)
  });
  const text = await response.text();
  const body = parseResponseBody(text);
  if (!response.ok) {
    const detail =
      typeof body === "object" && body && "detail" in body
        ? stringifyErrorDetail((body as { detail: unknown }).detail)
        : response.statusText;
    throw new Error(`${response.status}: ${detail}`);
  }
  return body as {
    access_token: string;
    token_type: string;
    expires_in: number;
    principal: AuthenticatedPrincipalRecord;
  };
}

export async function requestPasswordReset(
  input: PasswordResetRequestInput,
  apiBaseUrl = ""
): Promise<{ detail: string }> {
  const base = apiBaseUrl.replace(/\/$/, "");
  const response = await fetch(`${base}/api/public/password-reset/request`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  const text = await response.text();
  const body = parseResponseBody(text);
  if (!response.ok) {
    const detail =
      typeof body === "object" && body && "detail" in body
        ? stringifyErrorDetail((body as { detail: unknown }).detail)
        : response.statusText;
    throw new Error(`${response.status}: ${detail}`);
  }
  return body as { detail: string };
}

export async function confirmPasswordReset(
  input: PasswordResetConfirmInput,
  apiBaseUrl = ""
): Promise<{ detail: string }> {
  const base = apiBaseUrl.replace(/\/$/, "");
  const response = await fetch(`${base}/api/public/password-reset/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  const text = await response.text();
  const body = parseResponseBody(text);
  if (!response.ok) {
    const detail =
      typeof body === "object" && body && "detail" in body
        ? stringifyErrorDetail((body as { detail: unknown }).detail)
        : response.statusText;
    throw new Error(`${response.status}: ${detail}`);
  }
  return body as { detail: string };
}

export async function completeOnboarding(
  credentials: Credentials,
  tenantId: string
): Promise<AuthenticatedPrincipalRecord> {
  return request<AuthenticatedPrincipalRecord>(
    credentials,
    `/api/app/onboarding/${encodeURIComponent(tenantId)}/complete`,
    { method: "POST" }
  );
}

export async function updateTenantUserSettings(
  credentials: Credentials,
  tenantId: string,
  payload: TenantUserSettingsUpdatePayload,
): Promise<AuthenticatedPrincipalRecord> {
  return request<AuthenticatedPrincipalRecord>(
    credentials,
    `/api/app/tenants/${encodeURIComponent(tenantId)}/me/settings`,
    {
      method: "PUT",
      body: JSON.stringify(payload),
    },
  );
}

export async function updateAuthenticatedUserProfile(
  credentials: Credentials,
  payload: TenantUserProfileUpdatePayload,
): Promise<AuthenticatedPrincipalRecord> {
  return request<AuthenticatedPrincipalRecord>(credentials, "/api/app/me/profile", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function changeTenantUserPassword(
  credentials: Credentials,
  payload: TenantUserPasswordChangePayload,
): Promise<AuthenticatedPrincipalRecord> {
  return request<AuthenticatedPrincipalRecord>(credentials, "/api/app/me/password", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function getTenantDeliverySummary(
  credentials: Credentials,
  tenantId: string
): Promise<DeliverySummaryRecord> {
  return request<DeliverySummaryRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/delivery-summary`
  );
}

export function listTenantInvites(credentials: Credentials, tenantId: string): Promise<{ items: TenantInviteRecord[] }> {
  return request(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/invites`);
}

export function createTenantInvite(
  credentials: Credentials,
  tenantId: string,
  payload: TenantInviteCreatePayload
): Promise<TenantInviteRecord> {
  return request<{ invite: TenantInviteRecord } | TenantInviteRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/invites`,
    {
      method: "POST",
      body: JSON.stringify(payload)
    }
  ).then((result) => ("invite" in result ? result.invite : result));
}

export function resendTenantInvite(
  credentials: Credentials,
  tenantId: string,
  inviteId: string
): Promise<TenantInviteRecord> {
  return request<{ invite: TenantInviteRecord }>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/invites/${encodeURIComponent(inviteId)}/resend`,
    { method: "POST" }
  ).then((result) => result.invite);
}

export function revokeTenantInvite(
  credentials: Credentials,
  tenantId: string,
  inviteId: string
): Promise<TenantInviteRecord> {
  return request<{ invite: TenantInviteRecord }>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/invites/${encodeURIComponent(inviteId)}/revoke`,
    { method: "POST" }
  ).then((result) => result.invite);
}

export function listTenantTeams(credentials: Credentials, tenantId: string): Promise<TenantTeamRecord[]> {
  return request(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/teams`);
}

export function createTenantTeam(
  credentials: Credentials,
  tenantId: string,
  payload: TenantTeamCreatePayload
): Promise<TenantTeamRecord> {
  return request(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/teams`, {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function updateTenantTeamRecord(
  credentials: Credentials,
  tenantId: string,
  teamId: string,
  payload: TenantTeamCreatePayload
): Promise<TenantTeamRecord> {
  return request(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/teams/${encodeURIComponent(teamId)}`,
    {
      method: "PUT",
      body: JSON.stringify(payload)
    }
  );
}

export function listTenantMembers(credentials: Credentials, tenantId: string): Promise<TenantMemberRecord[]> {
  return request(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/members`);
}

export function updateTenantMemberRecord(
  credentials: Credentials,
  tenantId: string,
  membershipId: string,
  payload: TenantMemberUpdatePayload
): Promise<TenantMemberRecord> {
  return request(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/members/${encodeURIComponent(membershipId)}`,
    {
      method: "PUT",
      body: JSON.stringify(payload)
    }
  );
}

export function getTenantDiscordIdentity(
  credentials: Credentials,
  tenantId: string
): Promise<TenantDiscordIdentityRecord> {
  return request(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/discord/identity`);
}

export function startTenantDiscordLink(
  credentials: Credentials,
  tenantId: string,
  redirectTo = "/get-started"
): Promise<{ authorize_url: string }> {
  const query = new URLSearchParams({ redirect_to: redirectTo });
  return request(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/discord/link/start?${query.toString()}`,
    { method: "POST" }
  );
}

export function createTenantDiscordInvite(
  credentials: Credentials,
  tenantId: string
): Promise<TenantDiscordInviteRecord> {
  return request(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/discord/onboarding-invite`, {
    method: "POST"
  });
}

export function listTenants(credentials: Credentials): Promise<TenantRecord[]> {
  return request<TenantRecord[]>(credentials, "/api/admin/tenants");
}

export function getTenant(credentials: Credentials, tenantId: string): Promise<TenantRecord> {
  return request<TenantRecord>(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}`);
}

export function listCodexModels(
  credentials: Credentials,
  options?: { runtimeKind?: string | null; profileName?: string | null }
): Promise<CodexModelCatalogRecord> {
  const query = new URLSearchParams();
  if (options?.runtimeKind) {
    query.set("runtime_kind", options.runtimeKind);
  }
  if (options?.profileName) {
    query.set("profile_name", options.profileName);
  }
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  return request<CodexModelCatalogRecord>(credentials, `/api/admin/codex/models${suffix}`);
}

export function createTenant(
  credentials: Credentials,
  payload: TenantCreatePayload
): Promise<TenantRecord> {
  return request<TenantRecord>(credentials, "/api/admin/tenants", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function updateTenant(
  credentials: Credentials,
  tenantId: string,
  payload: TenantUpdatePayload
): Promise<TenantRecord> {
  return request<TenantRecord>(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}`, {
    method: "PUT",
    body: JSON.stringify(payload)
  });
}

export async function deleteTenant(credentials: Credentials, tenantId: string): Promise<void> {
  await request<void>(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}`, {
    method: "DELETE"
  });
}

export function archiveTenant(credentials: Credentials, tenantId: string): Promise<TenantRecord> {
  return request<TenantRecord>(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/archive`, {
    method: "POST"
  });
}

export function unarchiveTenant(credentials: Credentials, tenantId: string): Promise<TenantRecord> {
  return request<TenantRecord>(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/unarchive`, {
    method: "POST"
  });
}

export function testJira(
  credentials: Credentials,
  tenantId: string
): Promise<{ ok: boolean; details: string }> {
  return request(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/test-jira`, {
    method: "POST"
  });
}

export function startJiraConnect(
  credentials: Credentials,
  options?: { returnTo?: "wizard" | "edit"; tenantId?: string }
): Promise<{ authorize_url: string; expires_at: string }> {
  const query = new URLSearchParams();
  if (options?.returnTo) {
    query.set("return_to", options.returnTo);
  }
  if (options?.tenantId) {
    query.set("tenant_id", options.tenantId);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request(credentials, `/api/admin/jira/connect/start${suffix}`, {
    method: "POST"
  });
}

export function listJiraProjects(
  credentials: Credentials,
  connectionId: string
): Promise<JiraProjectRecord[]> {
  return request<JiraProjectRecord[]>(
    credentials,
    `/api/admin/jira/connections/${encodeURIComponent(connectionId)}/projects`
  );
}

export function getJiraWebhookDiagnostics(
  credentials: Credentials,
  tenantId: string,
  withinMinutes = 60
): Promise<JiraWebhookDiagnosticsRecord> {
  const query = new URLSearchParams({ within_minutes: String(withinMinutes) });
  return request<JiraWebhookDiagnosticsRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/jira/webhooks/diagnostics?${query.toString()}`
  );
}

export function provisionJiraWebhook(
  credentials: Credentials,
  tenantId: string
): Promise<JiraWebhookActionResult> {
  return request<JiraWebhookActionResult>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/jira/webhooks/provision`,
    { method: "POST" }
  );
}

export function resetJiraWebhook(
  credentials: Credentials,
  tenantId: string
): Promise<JiraWebhookActionResult> {
  return request<JiraWebhookActionResult>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/jira/webhooks/reset`,
    { method: "POST" }
  );
}

export function disconnectJira(
  credentials: Credentials,
  tenantId: string
): Promise<JiraWebhookActionResult> {
  return request<JiraWebhookActionResult>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/jira/disconnect`,
    { method: "POST" }
  );
}

export function previewReadyGate(
  credentials: Credentials,
  tenantId: string,
  maxResults = 10
): Promise<ReadyGatePreviewRecord> {
  const query = new URLSearchParams({ max_results: String(maxResults) });
  return request<ReadyGatePreviewRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/ready-preview?${query.toString()}`
  );
}

export function testGithub(
  credentials: Credentials,
  tenantId: string
): Promise<{ ok: boolean; details: string }> {
  return request(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/test-github`, {
    method: "POST"
  });
}

export function disconnectGitHub(
  credentials: Credentials,
  tenantId: string
): Promise<TenantRecord> {
  return request<TenantRecord>(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/github/disconnect`, {
    method: "POST"
  });
}

export function startGitHubInstall(
  credentials: Credentials,
  tenantId: string,
  options?: { returnTo?: "edit" | "wizard" }
): Promise<{ install_url: string; expires_at: string }> {
  const query = new URLSearchParams();
  if (options?.returnTo) {
    query.set("return_to", options.returnTo);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/github/install/start${suffix}`, {
    method: "POST"
  });
}

export function startDiscordInstall(
  credentials: Credentials,
  tenantId: string,
  options?: { returnTo?: "edit" | "wizard" }
): Promise<DiscordInstallStartRecord> {
  const query = new URLSearchParams();
  if (options?.returnTo) {
    query.set("return_to", options.returnTo);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/discord/install/start${suffix}`, {
    method: "POST"
  });
}

export function listGitHubRepositories(
  credentials: Credentials,
  tenantId: string
): Promise<GitHubRepositoryRecord[]> {
  return request<GitHubRepositoryRecord[]>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/github/repositories`
  );
}

export function listProjects(credentials: Credentials, tenantId: string): Promise<ProjectRecord[]> {
  return request<ProjectRecord[]>(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects`);
}

export function createProject(
  credentials: Credentials,
  tenantId: string,
  payload: ProjectCreatePayload
): Promise<ProjectRecord> {
  return request<ProjectRecord>(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects`, {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function updateProject(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  payload: ProjectUpdatePayload
): Promise<ProjectRecord> {
  return request<ProjectRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}`,
    {
      method: "PUT",
      body: JSON.stringify(payload)
    }
  );
}

export function getProject(credentials: Credentials, tenantId: string, projectId: string): Promise<ProjectRecord> {
  return request<ProjectRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}`
  );
}

export function getProjectAutomations(
  credentials: Credentials,
  tenantId: string,
  projectId: string
): Promise<ProjectAutomationsRecord> {
  return request<ProjectAutomationsRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/automations`
  );
}

export function updateProjectAutomations(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  payload: ProjectAutomationsPayload
): Promise<ProjectAutomationsRecord> {
  return request<ProjectAutomationsRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/automations`,
    {
      method: "PUT",
      body: JSON.stringify(payload)
    }
  );
}

export function runProjectAutomationNow(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  kind: string
): Promise<ProjectAutomationsRecord> {
  return request<ProjectAutomationsRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/automations/${encodeURIComponent(kind)}/run-now`,
    {
      method: "POST"
    }
  );
}

export function listProjectKnowledgeAssets(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  options?: {
    limit?: number;
    offset?: number;
    status?: string;
    sourceType?: string;
    query?: string;
  }
): Promise<ProjectKnowledgeAssetPageRecord> {
  const query = new URLSearchParams();
  if (options?.limit !== undefined) {
    query.set("limit", String(options.limit));
  }
  if (options?.offset !== undefined) {
    query.set("offset", String(options.offset));
  }
  if (options?.status) {
    query.set("status", options.status);
  }
  if (options?.sourceType) {
    query.set("source_type", options.sourceType);
  }
  if (options?.query) {
    query.set("q", options.query);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<ProjectKnowledgeAssetPageRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/knowledge/assets${suffix}`
  );
}

export function createProjectKnowledgeAsset(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  payload: ProjectKnowledgeAssetCreatePayload
): Promise<ProjectKnowledgeAssetRecord> {
  return request<ProjectKnowledgeAssetRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/knowledge/assets`,
    {
      method: "POST",
      body: JSON.stringify(payload)
    }
  );
}

export async function deleteProjectKnowledgeAsset(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  assetId: string
): Promise<void> {
  await request<void>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/knowledge/assets/${encodeURIComponent(assetId)}`,
    {
      method: "DELETE"
    }
  );
}

export function updateProjectKnowledgeAssetStatus(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  assetId: string,
  payload: ProjectKnowledgeAssetStatusUpdatePayload
): Promise<ProjectKnowledgeAssetRecord> {
  return request<ProjectKnowledgeAssetRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/knowledge/assets/${encodeURIComponent(assetId)}/status`,
    {
      method: "PATCH",
      body: JSON.stringify(payload)
    }
  );
}

export function getProjectKnowledgeAsset(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  assetId: string
): Promise<ProjectKnowledgeAssetDetailRecord> {
  return request<ProjectKnowledgeAssetDetailRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/knowledge/assets/${encodeURIComponent(assetId)}`
  );
}

export function getProjectKnowledgeStats(
  credentials: Credentials,
  tenantId: string,
  projectId: string
): Promise<ProjectKnowledgeStatsRecord> {
  return request<ProjectKnowledgeStatsRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/knowledge/stats`
  );
}

export function listProjectKnowledgeChunks(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  assetId: string,
  options?: { limit?: number; offset?: number }
): Promise<ProjectKnowledgeChunkPageRecord> {
  const query = new URLSearchParams();
  if (options?.limit !== undefined) {
    query.set("limit", String(options.limit));
  }
  if (options?.offset !== undefined) {
    query.set("offset", String(options.offset));
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<ProjectKnowledgeChunkPageRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/knowledge/assets/${encodeURIComponent(assetId)}/chunks${suffix}`
  );
}

export function debugProjectKnowledgeSearch(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  queryText: string,
  limit = 5
): Promise<ProjectKnowledgeDebugSearchRecord> {
  const query = new URLSearchParams();
  query.set("query", queryText);
  query.set("limit", String(limit));
  return request<ProjectKnowledgeDebugSearchRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/knowledge/debug-search?${query.toString()}`
  );
}

export function listProjectKnowledgeSources(
  credentials: Credentials,
  tenantId: string,
  projectId: string
): Promise<ProjectKnowledgeSourcePageRecord> {
  return request<ProjectKnowledgeSourcePageRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/knowledge/sources`
  );
}

export function createProjectKnowledgeSource(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  payload: ProjectKnowledgeSourceCreatePayload
): Promise<ProjectKnowledgeSourceRecord> {
  return request<ProjectKnowledgeSourceRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/knowledge/sources`,
    {
      method: "POST",
      body: JSON.stringify(payload)
    }
  );
}

export function updateProjectKnowledgeSource(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  sourceId: string,
  payload: ProjectKnowledgeSourceUpdatePayload
): Promise<ProjectKnowledgeSourceRecord> {
  return request<ProjectKnowledgeSourceRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/knowledge/sources/${encodeURIComponent(sourceId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload)
    }
  );
}

export function deleteProjectKnowledgeSource(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  sourceId: string
): Promise<void> {
  return request<void>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/knowledge/sources/${encodeURIComponent(sourceId)}`,
    {
      method: "DELETE"
    }
  );
}

export function syncProjectKnowledgeSource(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  sourceId: string
): Promise<ProjectKnowledgeSyncResult> {
  return request<ProjectKnowledgeSyncResult>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/knowledge/sources/${encodeURIComponent(sourceId)}/sync`,
    {
      method: "POST"
    }
  );
}

export function syncProjectKnowledgeFromJira(
  credentials: Credentials,
  tenantId: string,
  projectId: string
): Promise<ProjectKnowledgeSyncResult> {
  return request<ProjectKnowledgeSyncResult>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/knowledge/sync-jira`,
    {
      method: "POST"
    }
  );
}

export function getKnowledgeJiraSyncRuntimeStatus(
  credentials: Credentials
): Promise<KnowledgeJiraSyncRuntimeRecord> {
  return request<KnowledgeJiraSyncRuntimeRecord>(
    credentials,
    "/api/admin/observability/knowledge-jira-sync"
  );
}

export function getPlatformStatus(credentials: Credentials): Promise<PlatformStatusRecord> {
  return request<PlatformStatusRecord>(credentials, "/api/admin/status");
}

export function listRuns(
  credentials: Credentials,
  params: {
    tenantId?: string;
    projectId?: string;
    status?: RunStatus;
    issue?: string;
    prState?: "none" | "has_value";
    from?: string;
    to?: string;
    limit?: number;
    offset?: number;
  }
): Promise<RunRecord[]> {
  const query = new URLSearchParams();
  if (params.tenantId) {
    query.set("tenant_id", params.tenantId);
  }
  if (params.projectId) {
    query.set("project_id", params.projectId);
  }
  if (params.status) {
    query.set("status", params.status);
  }
  if (params.issue) {
    query.set("issue", params.issue);
  }
  if (params.prState) {
    query.set("pr_state", params.prState);
  }
  if (params.from) {
    query.set("from", params.from);
  }
  if (params.to) {
    query.set("to", params.to);
  }
  if (typeof params.limit === "number") {
    query.set("limit", String(params.limit));
  }
  if (typeof params.offset === "number") {
    query.set("offset", String(params.offset));
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<RunRecord[]>(credentials, `/api/admin/runs${suffix}`);
}

export function getRun(credentials: Credentials, runId: string): Promise<RunRecord> {
  return request<RunRecord>(credentials, `/api/admin/runs/${encodeURIComponent(runId)}`);
}

export function rerunRun(
  credentials: Credentials,
  runId: string,
  payload: RunRerunPayload = { mode: "fresh" }
): Promise<RunRecord> {
  return request<RunRecord>(credentials, `/api/admin/runs/${encodeURIComponent(runId)}/rerun`, {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function cancelRun(credentials: Credentials, runId: string): Promise<RunRecord> {
  return request<RunRecord>(credentials, `/api/admin/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST"
  });
}

export function listRunEvents(
  credentials: Credentials,
  runId: string,
  params: { limit?: number } = {}
): Promise<RunEventRecord[]> {
  const query = new URLSearchParams();
  if (params.limit) {
    query.set("limit", String(params.limit));
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<RunEventRecord[]>(
    credentials,
    `/api/admin/runs/${encodeURIComponent(runId)}/events${suffix}`
  );
}

export function listRunLogs(
  credentials: Credentials,
  runId: string,
  params: { limit?: number; beforeRecordedAt?: string; beforeEventId?: string } = {}
): Promise<RunLogEventRecord[]> {
  const query = new URLSearchParams();
  if (params.limit) {
    query.set("limit", String(params.limit));
  }
  if (params.beforeRecordedAt) {
    query.set("before_recorded_at", params.beforeRecordedAt);
  }
  if (params.beforeEventId) {
    query.set("before_event_id", params.beforeEventId);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<RunLogEventRecord[]>(
    credentials,
    `/api/admin/runs/${encodeURIComponent(runId)}/logs${suffix}`
  );
}

export function getTokenTimeline(
  credentials: Credentials,
  runId: string,
  params: {
    tenantId: string;
    stage?: string;
    attempt?: number;
    include_retries?: boolean;
    model?: string;
  }
): Promise<TokenTimelineRecord> {
  const query = new URLSearchParams();
  if (params.stage) {
    query.set("stage", params.stage);
  }
  if (typeof params.attempt === "number") {
    query.set("attempt", String(params.attempt));
  }
  if (params.include_retries !== undefined) {
    query.set("include_retries", params.include_retries ? "true" : "false");
  }
  if (params.model) {
    query.set("model", params.model);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<TokenTimelineRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(params.tenantId)}/runs/${encodeURIComponent(runId)}/token-timeline${suffix}`
  );
}

export function getTokenOverview(
  credentials: Credentials,
  params: {
    tenant_id: string;
    project_id: string;
    issue_key?: string;
    run_status?: string;
    stage?: string;
    attempt?: number;
    model?: string;
    start_date?: string;
    end_date?: string;
    only_retried?: boolean;
    only_with_test_stage?: boolean;
    page?: number;
    page_size?: number;
  }
): Promise<TokenOverviewRecord> {
  const query = new URLSearchParams();
  query.set("project_id", params.project_id);
  if (params.issue_key) {
    query.set("issue_key", params.issue_key);
  }
  if (params.run_status) {
    query.set("run_status", params.run_status);
  }
  if (params.stage) {
    query.set("stage", params.stage);
  }
  if (typeof params.attempt === "number") {
    query.set("attempt", String(params.attempt));
  }
  if (params.model) {
    query.set("model", params.model);
  }
  if (params.start_date) {
    query.set("start_date", params.start_date);
  }
  if (params.end_date) {
    query.set("end_date", params.end_date);
  }
  if (params.only_retried !== undefined) {
    query.set("only_retried", params.only_retried ? "true" : "false");
  }
  if (params.only_with_test_stage !== undefined) {
    query.set("only_with_test_stage", params.only_with_test_stage ? "true" : "false");
  }
  if (typeof params.page === "number") {
    query.set("page", String(params.page));
  }
  if (typeof params.page_size === "number") {
    query.set("page_size", String(params.page_size));
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<TokenOverviewRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(params.tenant_id)}/token-overview${suffix}`
  );
}

export function compareRunsTokens(
  credentials: Credentials,
  payload: TokenCompareRequestBody,
  options: {
    tenantId: string;
    projectId: string;
  }
): Promise<TokenCompareRecord> {
  const query = new URLSearchParams();
  query.set("project_id", options.projectId);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<TokenCompareRecord>(credentials, `/api/admin/tenants/${encodeURIComponent(options.tenantId)}/token-compare${suffix}`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function compareTokens(
  credentials: Credentials,
  payload: TokenCompareRequestBody,
  options: {
    tenantId: string;
    projectId: string;
  }
): Promise<TokenCompareRecord> {
  return compareRunsTokens(credentials, payload, options);
}

export function getTokenStageDiagnostics(
  credentials: Credentials,
  params: {
    tenant_id: string;
    project_id: string;
    issue_key?: string;
    run_status?: string;
    stage?: string;
    attempt?: number;
    model?: string;
    start_date?: string;
    end_date?: string;
    only_retried?: boolean;
    only_with_test_stage?: boolean;
    page?: number;
    page_size?: number;
  }
): Promise<TokenStageDiagnosticsRecord> {
  const query = new URLSearchParams();
  query.set("project_id", params.project_id);
  if (params.issue_key) {
    query.set("issue_key", params.issue_key);
  }
  if (params.run_status) {
    query.set("run_status", params.run_status);
  }
  if (params.stage) {
    query.set("stage", params.stage);
  }
  if (typeof params.attempt === "number") {
    query.set("attempt", String(params.attempt));
  }
  if (params.model) {
    query.set("model", params.model);
  }
  if (params.start_date) {
    query.set("start_date", params.start_date);
  }
  if (params.end_date) {
    query.set("end_date", params.end_date);
  }
  if (params.only_retried !== undefined) {
    query.set("only_retried", params.only_retried ? "true" : "false");
  }
  if (params.only_with_test_stage !== undefined) {
    query.set("only_with_test_stage", params.only_with_test_stage ? "true" : "false");
  }
  if (typeof params.page === "number") {
    query.set("page", String(params.page));
  }
  if (typeof params.page_size === "number") {
    query.set("page_size", String(params.page_size));
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<TokenStageDiagnosticsRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(params.tenant_id)}/token-stage-diagnostics${suffix}`
  );
}

export function getTokenStageDiagnosticsCompare(
  credentials: Credentials,
  params: {
    tenant_id: string;
    project_ids: string[];
    issue_key?: string;
    run_status?: string;
    stage?: string;
    attempt?: number;
    model?: string;
    start_date?: string;
    end_date?: string;
    only_retried?: boolean;
    only_with_test_stage?: boolean;
  }
): Promise<TokenStageDiagnosticsCompareRecord> {
  const query = new URLSearchParams();
  query.set("project_ids", params.project_ids.join(","));
  if (params.issue_key) {
    query.set("issue_key", params.issue_key);
  }
  if (params.run_status) {
    query.set("run_status", params.run_status);
  }
  if (params.stage) {
    query.set("stage", params.stage);
  }
  if (typeof params.attempt === "number") {
    query.set("attempt", String(params.attempt));
  }
  if (params.model) {
    query.set("model", params.model);
  }
  if (params.start_date) {
    query.set("start_date", params.start_date);
  }
  if (params.end_date) {
    query.set("end_date", params.end_date);
  }
  if (params.only_retried !== undefined) {
    query.set("only_retried", params.only_retried ? "true" : "false");
  }
  if (params.only_with_test_stage !== undefined) {
    query.set("only_with_test_stage", params.only_with_test_stage ? "true" : "false");
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<TokenStageDiagnosticsCompareRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(params.tenant_id)}/token-stage-diagnostics-compare${suffix}`
  );
}

export async function streamRunEvents(
  credentials: Credentials,
  runId: string,
  onEvent: (event: RunEventRecord | (RunLogEventRecord & { event_kind?: string })) => void,
  signal?: AbortSignal
): Promise<void> {
  void credentials;
  const response = await fetch(`/api/bff/api/admin/runs/${encodeURIComponent(runId)}/events/stream`, {
    method: "GET",
    headers: {
      Accept: "application/x-ndjson"
    },
    signal
  });
  if (!response.ok || !response.body) {
    throw new Error(`${response.status}: unable to open run event stream`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (line) {
          try {
            const payload = JSON.parse(line) as RunEventRecord;
            onEvent(payload);
          } catch {
            // Ignore malformed stream lines.
          }
        }
        newline = buffer.indexOf("\n");
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export function listManagedSecrets(credentials: Credentials): Promise<ManagedSecretRecord[]> {
  return request<ManagedSecretRecord[]>(credentials, "/api/admin/secrets");
}

export function listTenantManagedSecrets(credentials: Credentials, tenantId: string): Promise<ManagedSecretRecord[]> {
  return request<ManagedSecretRecord[]>(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/secrets`);
}

export function listDiscordAllowlistRequests(
  credentials: Credentials,
  tenantId: string,
  projectId: string
): Promise<DiscordAllowlistRequestRecord[]> {
  return request<DiscordAllowlistRequestRecord[]>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/discord/allowlist-requests`
  );
}

export function approveDiscordAllowlistRequest(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  userId: string
): Promise<DiscordAllowlistApprovalResult> {
  return request<DiscordAllowlistApprovalResult>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/discord/allowlist-requests/${encodeURIComponent(userId)}/approve`,
    {
      method: "POST"
    }
  );
}

export function upsertManagedSecret(
  credentials: Credentials,
  secretRef: string,
  value: string
): Promise<ManagedSecretRecord> {
  return request<ManagedSecretRecord>(credentials, `/api/admin/secrets/${encodeURIComponent(secretRef)}`, {
    method: "PUT",
    body: JSON.stringify({ value })
  });
}

export async function deleteManagedSecret(credentials: Credentials, secretRef: string): Promise<void> {
  await request<void>(credentials, `/api/admin/secrets/${encodeURIComponent(secretRef)}`, {
    method: "DELETE"
  });
}

export function resolveManagedSecret(
  credentials: Credentials,
  secretRef: string
): Promise<ManagedSecretResolveResult> {
  return request<ManagedSecretResolveResult>(credentials, "/api/admin/secrets/resolve", {
    method: "POST",
    body: JSON.stringify({ secret_ref: secretRef })
  });
}

export function getAgentRuntimeRouting(credentials: Credentials): Promise<AgentRuntimeRoutingRecord> {
  return request<AgentRuntimeRoutingRecord>(credentials, "/api/admin/agent-runtimes");
}

export function updateAgentRuntimeRouting(
  credentials: Credentials,
  payload: Pick<AgentRuntimeRoutingRecord, "role_routing" | "name_routing" | "selector_routing">
): Promise<AgentRuntimeRoutingRecord> {
  return request<AgentRuntimeRoutingRecord>(credentials, "/api/admin/agent-runtimes", {
    method: "PUT",
    body: JSON.stringify(payload)
  });
}

export function resetAgentRuntimeRouting(credentials: Credentials): Promise<AgentRuntimeRoutingRecord> {
  return request<AgentRuntimeRoutingRecord>(credentials, "/api/admin/agent-runtimes/reset", {
    method: "POST"
  });
}

export function listAgentRuntimeProfiles(credentials: Credentials): Promise<AgentExecutionProfilesRecord> {
  return request<AgentExecutionProfilesRecord>(credentials, "/api/admin/agent-runtime-profiles");
}

export function listAgentRuntimeTools(credentials: Credentials): Promise<AgentRuntimeToolsRecord> {
  return request<AgentRuntimeToolsRecord>(credentials, "/api/admin/agent-runtime-tools");
}

export function createAgentRuntimeProfile(
  credentials: Credentials,
  payload: AgentExecutionProfileCreatePayload
): Promise<AgentExecutionProfileRecord> {
  return request<AgentExecutionProfileRecord>(credentials, "/api/admin/agent-runtime-profiles", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function updateAgentRuntimeProfile(
  credentials: Credentials,
  profileName: string,
  payload: AgentExecutionProfileWritePayload
): Promise<AgentExecutionProfileRecord> {
  return request<AgentExecutionProfileRecord>(
    credentials,
    `/api/admin/agent-runtime-profiles/${encodeURIComponent(profileName)}`,
    {
      method: "PUT",
      body: JSON.stringify(payload)
    }
  );
}

export function resetAgentRuntimeProfile(
  credentials: Credentials,
  profileName: string
): Promise<AgentExecutionProfileRecord> {
  return request<AgentExecutionProfileRecord>(
    credentials,
    `/api/admin/agent-runtime-profiles/${encodeURIComponent(profileName)}/reset`,
    {
      method: "POST"
    }
  );
}

export function deleteAgentRuntimeProfile(
  credentials: Credentials,
  profileName: string
): Promise<AgentExecutionProfilesRecord> {
  return request<AgentExecutionProfilesRecord>(
    credentials,
    `/api/admin/agent-runtime-profiles/${encodeURIComponent(profileName)}`,
    {
      method: "DELETE"
    }
  );
}

export function upsertTenantManagedSecret(
  credentials: Credentials,
  tenantId: string,
  secretKey: string,
  value: string
): Promise<ManagedSecretRecord> {
  return request<ManagedSecretRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/secrets/${encodeURIComponent(secretKey)}`,
    {
      method: "PUT",
      body: JSON.stringify({ value })
    }
  );
}

export async function deleteTenantManagedSecret(
  credentials: Credentials,
  tenantId: string,
  secretKey: string
): Promise<void> {
  await request<void>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/secrets/${encodeURIComponent(secretKey)}`,
    {
      method: "DELETE"
    }
  );
}

export function resolveTenantManagedSecret(
  credentials: Credentials,
  tenantId: string,
  secretKey: string
): Promise<ManagedSecretResolveResult> {
  return request<ManagedSecretResolveResult>(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/secrets/resolve`, {
    method: "POST",
    body: JSON.stringify({ secret_ref: secretKey })
  });
}
