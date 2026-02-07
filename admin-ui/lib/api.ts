export type JiraConfig = {
  mcp_endpoint: string;
  auth_ref: string;
  project_keys: string[];
  ready_label: string;
  in_progress_label: string;
  blocked_label: string;
  done_label: string | null;
  ready_jql: string;
  webhook_secret_ref: string | null;
};

export type GithubConfig = {
  mode: string;
  app_id_ref: string;
  private_key_ref: string;
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
  tenant_id: string;
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

export type TenantRecord = TenantCreatePayload & {
  created_at: string;
  updated_at: string;
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

export type Credentials = {
  apiBaseUrl: string;
  username: string;
  password: string;
};

function authHeader(credentials: Credentials): string {
  if (!credentials.username || !credentials.password) {
    throw new Error("Username and password are required.");
  }
  return `Basic ${btoa(`${credentials.username}:${credentials.password}`)}`;
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
  tenantId: string
): Promise<{ install_url: string; expires_at: string }> {
  return request(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/github/install/start`, {
    method: "POST"
  });
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
