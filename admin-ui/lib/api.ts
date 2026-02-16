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
  allow_label_mutations: boolean;
  max_runtime_minutes: number;
  max_dev_test_review_loops: number;
  max_concurrent_runs: number;
  allowed_commands: string[];
  require_agents_md: boolean;
};

export type DiscordConfig = {
  guild_id?: string | null;
  channel_id?: string | null;
  notify_events: string[];
  allowed_user_ids?: string[];
  command_secret_ref?: string | null;
};

export type TenantCreatePayload = {
  name: string;
  is_enabled: boolean;
  jira: JiraConfig;
  github: GithubConfig;
  repos: ReposConfig;
  policy: PolicyConfig;
  discord: DiscordConfig | null;
};

export type TenantUpdatePayload = {
  name: string;
  is_enabled: boolean;
  jira: JiraConfig;
  github: GithubConfig;
  repos: ReposConfig;
  policy: PolicyConfig;
  discord: DiscordConfig | null;
};

export type TenantRecord = {
  tenant_id: string;
  name: string;
  is_enabled: boolean;
  jira: JiraConfig;
  github: GithubConfig;
  repos: ReposConfig;
  policy: PolicyConfig;
  discord: DiscordConfig | null;
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
    | "allow_label_mutations"
    | "max_runtime_minutes"
    | "max_dev_test_review_loops"
    | "max_concurrent_runs"
    | "allowed_commands"
    | "require_agents_md"
  >
>;

export type ProjectDiscordConfig = {
  channel_id?: string | null;
  notify_events?: string[];
  ask_thread_channel_ids?: string[];
  seed_followup_thread_channel_ids?: string[];
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
  status: string;
  last_error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  plan: Record<string, unknown> | null;
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
  working_dir: string | null;
  stage: string;
  attempt: number | null;
  stream: string;
  message: string;
  recorded_at: string;
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
  accessToken: string;
};

export type AdminLoginInput = {
  apiBaseUrl: string;
  username: string;
  password: string;
};

function authHeader(credentials: Credentials): string {
  if (!credentials.accessToken) {
    throw new Error("Admin access token is required.");
  }
  return `Bearer ${credentials.accessToken}`;
}

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

async function request<T>(
  credentials: Credentials,
  path: string,
  init?: RequestInit
): Promise<T> {
  const base = credentials.apiBaseUrl.replace(/\/$/, "");
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: authHeader(credentials),
      ...(init?.headers ?? {})
    }
  });

  const text = await response.text();
  const body = parseResponseBody(text);

  if (!response.ok) {
    const detail =
      typeof body === "object" && body && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : response.statusText;
    throw new Error(`${response.status}: ${detail}`);
  }

  return body as T;
}

export async function verifyAdminCredentials(credentials: Credentials): Promise<void> {
  await request<{ username: string }>(credentials, "/api/admin/auth/me");
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
        ? String((body as { detail: unknown }).detail)
        : response.statusText;
    throw new Error(`${response.status}: ${detail}`);
  }

  const parsed = body as { access_token?: string };
  if (!parsed.access_token) {
    throw new Error("Authentication failed: missing access token");
  }

  return {
    apiBaseUrl: base,
    accessToken: parsed.access_token
  };
}

export function listTenants(credentials: Credentials): Promise<TenantRecord[]> {
  return request<TenantRecord[]>(credentials, "/api/admin/tenants");
}

export function getTenant(credentials: Credentials, tenantId: string): Promise<TenantRecord> {
  return request<TenantRecord>(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}`);
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

export function listRuns(
  credentials: Credentials,
  params: {
    tenantId?: string;
    projectId?: string;
    status?: string;
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

export function rerunRun(credentials: Credentials, runId: string): Promise<RunRecord> {
  return request<RunRecord>(credentials, `/api/admin/runs/${encodeURIComponent(runId)}/rerun`, {
    method: "POST"
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

export async function streamRunEvents(
  credentials: Credentials,
  runId: string,
  onEvent: (event: RunEventRecord | (RunLogEventRecord & { event_kind?: string })) => void,
  signal?: AbortSignal
): Promise<void> {
  const base = credentials.apiBaseUrl.replace(/\/$/, "");
  const response = await fetch(`${base}/api/admin/runs/${encodeURIComponent(runId)}/events/stream`, {
    method: "GET",
    headers: {
      Authorization: authHeader(credentials),
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
