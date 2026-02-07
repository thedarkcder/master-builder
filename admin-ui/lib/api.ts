export type TenantRecord = {
  tenant_id: string;
  name: string;
  is_enabled: boolean;
  jira: Record<string, unknown>;
  github: Record<string, unknown>;
  repos: Record<string, unknown>;
  policy: Record<string, unknown>;
  discord: Record<string, unknown> | null;
};

export type RunRecord = {
  run_id: string;
  tenant_id: string;
  issue_key: string;
  status: string;
  branch: string | null;
  pr_url: string | null;
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
  const body = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const detail = body?.detail || response.statusText;
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

export function createTenant(credentials: Credentials, payload: unknown): Promise<TenantRecord> {
  return request<TenantRecord>(credentials, "/api/admin/tenants", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function updateTenant(
  credentials: Credentials,
  tenantId: string,
  payload: unknown
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

export function testJira(credentials: Credentials, tenantId: string): Promise<{ ok: boolean; details: string }> {
  return request(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/test-jira`, {
    method: "POST"
  });
}

export function testGithub(credentials: Credentials, tenantId: string): Promise<{ ok: boolean; details: string }> {
  return request(credentials, `/api/admin/tenants/${encodeURIComponent(tenantId)}/test-github`, {
    method: "POST"
  });
}

export function listRuns(
  credentials: Credentials,
  params: { tenantId?: string; status?: string }
): Promise<RunRecord[]> {
  const query = new URLSearchParams();
  if (params.tenantId) query.set("tenant_id", params.tenantId);
  if (params.status) query.set("status", params.status);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<RunRecord[]>(credentials, `/api/admin/runs${suffix}`);
}

export function getRun(credentials: Credentials, runId: string): Promise<RunRecord> {
  return request<RunRecord>(credentials, `/api/admin/runs/${encodeURIComponent(runId)}`);
}
