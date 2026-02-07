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
  mode: string;
  webhook_secret_ref: string | null;
  installation_id: string | null;
};

export type ReposConfig = {
  allowlist: string[];
  mapping_rules_by_project_key: Record<string, string>;
  mapping_rules_by_component: Record<string, string>;
  fallback_repo: string | null;
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
  channel_id: string | null;
  channel_name_template: string;
  notify_events: string[];
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
  issue_key: string;
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

export function listRuns(
  credentials: Credentials,
  params: { tenantId?: string; status?: string }
): Promise<RunRecord[]> {
  const query = new URLSearchParams();
  if (params.tenantId) {
    query.set("tenant_id", params.tenantId);
  }
  if (params.status) {
    query.set("status", params.status);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<RunRecord[]>(credentials, `/api/admin/runs${suffix}`);
}

export function getRun(credentials: Credentials, runId: string): Promise<RunRecord> {
  return request<RunRecord>(credentials, `/api/admin/runs/${encodeURIComponent(runId)}`);
}

export function listManagedSecrets(credentials: Credentials): Promise<ManagedSecretRecord[]> {
  return request<ManagedSecretRecord[]>(credentials, "/api/admin/secrets");
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

export function resolveManagedSecret(
  credentials: Credentials,
  secretRef: string
): Promise<ManagedSecretResolveResult> {
  return request<ManagedSecretResolveResult>(credentials, "/api/admin/secrets/resolve", {
    method: "POST",
    body: JSON.stringify({ secret_ref: secretRef })
  });
}
