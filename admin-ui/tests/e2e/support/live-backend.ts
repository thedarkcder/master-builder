import type { APIRequestContext } from "@playwright/test";
import { execFileSync } from "node:child_process";

export function requireLiveSetting(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required for disposable live-backend tests`);
  return value;
}

export const API_BASE_URL = process.env.PLAYWRIGHT_API_BASE_URL ?? "http://localhost:60001";

export type TenantProjectRecord = {
  project_id: string;
  tenant_id: string;
  name: string;
};

export type TenantProjectCreatePayload = {
  name: string;
  github_repository: string;
  jira_project_key: string;
  environment?: Record<string, string>;
  secret_refs?: Record<string, string>;
};

function psycopgDatabaseUrl(): string {
  const databaseUrl = requireLiveSetting("ORCHESTRATOR_DATABASE_URL");
  return databaseUrl.replace("postgresql+psycopg://", "postgresql://");
}

export async function loginTenantUser(
  request: APIRequestContext,
  email: string,
  password: string,
): Promise<{ accessToken: string; tenantId: string }> {
  const response = await request.post(`${API_BASE_URL}/api/app/auth/login`, {
    data: { email, password },
  });
  if (!response.ok()) {
    throw new Error(`Tenant login failed: ${response.status()} ${await response.text()}`);
  }
  const payload = (await response.json()) as {
    access_token: string;
    principal: { memberships: Array<{ tenant_id: string }> };
  };
  const tenantId = payload.principal.memberships[0]?.tenant_id;
  if (!tenantId) {
    throw new Error("Tenant login returned no memberships");
  }
  return { accessToken: payload.access_token, tenantId };
}

export async function archiveTenant(
  request: APIRequestContext,
  tenantId: string,
  accessToken?: string,
): Promise<void> {
  const username = process.env.ORCHESTRATOR_ADMIN_USERNAME ?? "admin";
  const password = accessToken ? "" : requireLiveSetting("ORCHESTRATOR_ADMIN_PASSWORD");
  const basicAuth = Buffer.from(`${username}:${password}`).toString("base64");
  const response = await request.post(`${API_BASE_URL}/api/admin/tenants/${tenantId}/archive`, {
    headers: {
      Authorization: accessToken ? `Bearer ${accessToken}` : `Basic ${basicAuth}`,
    },
  });
  if (!response.ok()) {
    throw new Error(`Tenant archive failed: ${response.status()} ${await response.text()}`);
  }
}

export async function listTenantProjects(
  request: APIRequestContext,
  tenantId: string,
  accessToken: string,
): Promise<TenantProjectRecord[]> {
  const response = await request.get(`${API_BASE_URL}/api/admin/tenants/${tenantId}/projects`, {
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
  });
  if (!response.ok()) {
    throw new Error(`Project listing failed: ${response.status()} ${await response.text()}`);
  }
  return (await response.json()) as TenantProjectRecord[];
}

export async function createTenantProject(
  request: APIRequestContext,
  tenantId: string,
  accessToken: string,
  payload: TenantProjectCreatePayload,
): Promise<TenantProjectRecord> {
  const response = await request.post(`${API_BASE_URL}/api/admin/tenants/${tenantId}/projects`, {
    data: payload,
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
  });
  if (!response.ok()) {
    throw new Error(`Project creation failed: ${response.status()} ${await response.text()}`);
  }
  return (await response.json()) as TenantProjectRecord;
}

export function seedTenantProject(tenantId: string, name: string): TenantProjectRecord {
  const projectId = `${tenantId}-${name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}`;
  const nowIso = new Date().toISOString();
  const githubRepository = `https://github.com/example/${projectId}`;
  const jiraProjectKey = name.replace(/[^A-Za-z]/g, "").toUpperCase().slice(0, 6) || "PRJ";
  const script = `
import os
from datetime import datetime
import psycopg

with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into projects (
                project_id,
                tenant_id,
                name,
                github_repository,
                jira_project_key,
                policy_overrides,
                environment,
                secret_refs,
                discord_config,
                is_archived,
                created_at,
                updated_at
            ) values (
                %(project_id)s,
                %(tenant_id)s,
                %(name)s,
                %(github_repository)s,
                %(jira_project_key)s,
                '{}'::json,
                '{}'::json,
                '{}'::json,
                '{}'::json,
                false,
                %(created_at)s,
                %(updated_at)s
            )
            on conflict (project_id) do nothing
            """,
            {
                "project_id": os.environ["PROJECT_ID"],
                "tenant_id": os.environ["TENANT_ID"],
                "name": os.environ["PROJECT_NAME"],
                "github_repository": os.environ["GITHUB_REPOSITORY"],
                "jira_project_key": os.environ["JIRA_PROJECT_KEY"],
                "created_at": datetime.fromisoformat(os.environ["CREATED_AT"]),
                "updated_at": datetime.fromisoformat(os.environ["UPDATED_AT"]),
            },
        )
    connection.commit()
`;
  execFileSync("python3", ["-c", script], {
    env: {
      ...process.env,
      DATABASE_URL: psycopgDatabaseUrl(),
      PROJECT_ID: projectId,
      TENANT_ID: tenantId,
      PROJECT_NAME: name,
      GITHUB_REPOSITORY: githubRepository,
      JIRA_PROJECT_KEY: jiraProjectKey,
      CREATED_AT: nowIso,
      UPDATED_AT: nowIso,
    },
    stdio: "pipe",
  });
  return {
    project_id: projectId,
    tenant_id: tenantId,
    name,
  };
}
