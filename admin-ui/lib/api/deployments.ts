import { request, type Credentials } from "@/lib/api/http";

export type { Credentials } from "@/lib/api/http";

export type ProjectDeploymentDomainRecord = {
  key: string;
  host: string;
  path?: string | null;
  is_primary?: boolean;
  tls_enabled?: boolean;
};

export type ProjectDeploymentResourceRecord = {
  key: string;
  kind: string;
  name?: string | null;
  config?: Record<string, unknown>;
};

export type ProjectDeploymentBackupPolicyRecord = {
  key: string;
  resource_key: string;
  enabled?: boolean;
  schedule?: string | null;
  retention_days?: number | null;
  config?: Record<string, unknown>;
};

export type ProjectDeploymentConfigRecord = {
  enabled: boolean;
  environment_name: string | null;
  source_strategy: "dockerfile" | "docker_compose" | null;
  domains: ProjectDeploymentDomainRecord[];
  resources: ProjectDeploymentResourceRecord[];
  backup_policies: ProjectDeploymentBackupPolicyRecord[];
};

export type ProjectDeploymentReleaseCreatePayload = {
  git_ref?: string | null;
  commit_sha?: string | null;
  reason?: string | null;
};

export type ProjectDeploymentReleaseStatusUpdatePayload = {
  status: "queued" | "provisioning" | "deploying" | "live" | "failed" | "rolled_back";
  last_error?: string | null;
  deployment_uuid?: string | null;
};

export type ProjectDeploymentReleaseRecord = {
  release_id: string;
  tenant_id: string;
  project_id: string;
  provider: string;
  app_id?: string | null;
  status: string;
  environment_name: string | null;
  source_strategy: string | null;
  git_ref: string | null;
  commit_sha: string | null;
  requested_by_user_id: string | null;
  deployment_snapshot: Record<string, unknown>;
  provider_context: Record<string, unknown>;
  last_error: string | null;
  requested_at: string;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
};

export const PROJECT_APP_STATUSES = [
  "draft",
  "needs_pr_merge",
  "ready",
  "deploying",
  "live",
  "failed",
] as const;

export type ProjectAppStatus = (typeof PROJECT_APP_STATUSES)[number];

export const PROJECT_APP_ANALYSIS_RUN_STATUSES = ["queued", "running", "completed", "failed"] as const;

export type ProjectAppAnalysisRunStatus = (typeof PROJECT_APP_ANALYSIS_RUN_STATUSES)[number];

export type ProjectAppRecord = {
  app_id: string;
  tenant_id: string;
  project_id: string;
  name: string;
  slug: string;
  source_path: string;
  detection_confidence: number | null;
  detected_runtime: string | null;
  detected_language: string | null;
  analysis_source: string | null;
  build_strategy: "dockerfile" | "docker_compose" | "nixpacks" | string | null;
  exposed_port: number | null;
  healthcheck: string | Record<string, unknown> | null;
  start_command: string | null;
  env_schema_json: Record<string, unknown> | null;
  secret_schema_json: Record<string, unknown> | null;
  status: ProjectAppStatus | string;
  last_release_status?: string | null;
  last_release_id?: string | null;
  last_error?: string | null;
  created_at: string;
  updated_at: string;
};

export type ProjectAppAnalysisRunRecord = {
  analysis_run_id: string;
  tenant_id: string;
  project_id: string;
  status: ProjectAppAnalysisRunStatus | string;
  raw_result: Record<string, unknown> | null;
  last_error: string | null;
  requested_by_user_id: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string;
};

export type ProjectAppDeploymentConfigRecord = ProjectDeploymentConfigRecord & {
  app_id: string;
  build_strategy: "dockerfile" | "docker_compose" | "nixpacks" | string | null;
  exposed_port: number | null;
  healthcheck: string | Record<string, unknown> | null;
  start_command: string | null;
  env_schema_json: Record<string, unknown> | null;
  secret_schema_json: Record<string, unknown> | null;
};

export type ProjectAppDeploymentConfigUpdatePayload = {
  enabled: boolean;
  environment_name: string | null;
  source_strategy: "dockerfile" | "docker_compose" | null;
  build_strategy: "dockerfile" | "docker_compose" | "nixpacks" | null;
  exposed_port: number | null;
  healthcheck: string | Record<string, unknown> | null;
  start_command: string | null;
  env_schema_json: Record<string, unknown> | null;
  secret_schema_json: Record<string, unknown> | null;
  domains: ProjectDeploymentDomainRecord[];
  resources: ProjectDeploymentResourceRecord[];
  backup_policies: ProjectDeploymentBackupPolicyRecord[];
};

export type ProjectAppDeploymentReleaseRecord = ProjectDeploymentReleaseRecord & {
  app_id: string;
};

export type ProjectAppAnalysisRunCreatePayload = Record<string, never>;

export type TenantDeploymentsOverviewFailureRecord = {
  tenant_id: string;
  project_id: string;
  project_name: string;
  app_id: string;
  app_name: string;
  slug: string;
  source_path: string | null;
  status: string;
  last_error: string | null;
  last_release_id: string | null;
  updated_at: string;
};

export type TenantDeploymentsOverviewAppRecord = {
  tenant_id: string;
  tenant_name?: string | null;
  project_id: string;
  project_name: string;
  app_id: string;
  app_name: string;
  slug: string;
  source_path: string | null;
  status: string;
  detection_confidence: number | null;
  detected_runtime: string | null;
  detected_language: string | null;
  build_strategy: string | null;
  last_release_status: string | null;
  last_error: string | null;
  updated_at: string;
};

export type TenantDeploymentsOverviewRecord = {
  summary: {
    total_apps: number;
    draft_count: number;
    needs_pr_merge_count: number;
    ready_count: number;
    deploying_count: number;
    live_count: number;
    failed_count: number;
  };
  latest_failures: TenantDeploymentsOverviewFailureRecord[];
  apps: TenantDeploymentsOverviewAppRecord[];
  generated_at: string | null;
};

export type TenantDeploymentPlaneRecord = {
  provider: "internal_coolify";
  infrastructure_provider: "aws" | "hetzner" | null;
  region: string | null;
  base_domain: string | null;
  platform_subdomain: string | null;
  api_base_url: string | null;
  coolify_project_uuid: string | null;
  coolify_environment_name: string | null;
  coolify_server_uuid: string | null;
  coolify_destination_uuid: string | null;
  managed_host_id: string | null;
  secret_refs: Record<string, string>;
  state: "unconfigured" | "provisioning" | "active" | "degraded" | "paused" | "failed";
  last_error: string | null;
};

export type TenantDeploymentPlaneUpdatePayload = TenantDeploymentPlaneRecord;

export const DEPLOYMENT_HOST_STATES = ["provisioning", "active", "degraded", "offline", "retired"] as const;

export type DeploymentHostState = (typeof DEPLOYMENT_HOST_STATES)[number];

export type DeploymentHostRecord = {
  host_id: string;
  label: string;
  provider: string;
  infrastructure_provider: string | null;
  region: string | null;
  capabilities: string[];
  agent_version: string | null;
  metadata: Record<string, unknown>;
  state: DeploymentHostState | string;
  registered_at: string | null;
  last_seen_at: string | null;
  created_at: string;
  updated_at: string;
};

export type DeploymentHostCreatePayload = {
  label: string;
  provider?: "internal_coolify";
  infrastructure_provider?: "aws" | "hetzner" | null;
  region?: string | null;
  capabilities: string[];
};

export type DeploymentHostBootstrapRecord = {
  host: DeploymentHostRecord;
  bootstrap_token: string;
};

export type ProjectDeploymentOperationResultRecord = {
  ok: boolean;
  action: string;
  details: string;
  job_id?: string | null;
  status?: string | null;
  metadata?: Record<string, unknown>;
};

type ProjectDeploymentOperationApiItemRecord = {
  key: string;
  kind?: string | null;
  status: "applied" | "skipped" | "unsupported" | "failed";
  provider_uuid?: string | null;
  message?: string | null;
  details?: Record<string, unknown>;
};

type ProjectDeploymentOperationApiRecord = {
  operation: string;
  tenant_id: string;
  project_id: string;
  provider: string;
  application_uuid?: string | null;
  items: ProjectDeploymentOperationApiItemRecord[];
  applied_count: number;
  skipped_count: number;
  unsupported_count: number;
  failed_count: number;
  executed_at: string;
};

export type ProjectDeploymentApplyResourcesPayload = {
  resources: ProjectDeploymentResourceRecord[];
};

export type ProjectDeploymentApplyDomainsPayload = {
  domains: ProjectDeploymentDomainRecord[];
};

export type ProjectDeploymentApplyBackupsPayload = {
  backup_policies: ProjectDeploymentBackupPolicyRecord[];
};

export type ProjectDeploymentBackupNowPayload = {
  resource_key?: string | null;
  backup_policy_key?: string | null;
};

export type ProjectDeploymentRestoreRequestPayload = {
  backup_key: string;
  resource_key: string;
  backup_uuid?: string | null;
  execution_uuid: string;
  confirmation_value: string;
};

export type ProjectDeploymentBackupExecutionRecord = {
  execution_uuid: string;
  status: string | null;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  artifact_path: string | null;
  file_name: string | null;
  details: Record<string, unknown>;
};

export type ProjectDeploymentBackupExecutionListRecord = {
  backup_key: string;
  resource_key: string;
  backup_uuid: string | null;
  database_uuid: string | null;
  executions: ProjectDeploymentBackupExecutionRecord[];
};

export type ProjectDeploymentRestoreRunRecord = {
  restore_run_id: string;
  tenant_id: string;
  project_id: string;
  app_id: string;
  backup_policy_key: string;
  resource_key: string;
  backup_uuid: string | null;
  execution_uuid: string;
  database_type: "postgres" | "mysql" | "mariadb";
  database_uuid: string;
  restore_mode: "replace";
  requested_by_user_id: string | null;
  confirmation_value: string;
  execution_payload: Record<string, unknown>;
  status: "queued" | "running" | "succeeded" | "failed" | string;
  last_error: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string;
};


export function listProjectApps(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
): Promise<ProjectAppRecord[]> {
  return request<ProjectAppRecord[]>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps`
  );
}

export function getProjectApp(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  appId: string,
): Promise<ProjectAppRecord> {
  return request<ProjectAppRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}`
  );
}

export function createProjectApp(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  payload: Partial<ProjectAppRecord>,
): Promise<ProjectAppRecord> {
  return request<ProjectAppRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    }
  );
}

export function updateProjectApp(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  appId: string,
  payload: Partial<ProjectAppRecord>,
): Promise<ProjectAppRecord> {
  return request<ProjectAppRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}`,
    {
      method: "PUT",
      body: JSON.stringify(payload),
    }
  );
}

export function createProjectAppAnalysisRun(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  payload: ProjectAppAnalysisRunCreatePayload = {},
): Promise<ProjectAppAnalysisRunRecord> {
  return request<ProjectAppAnalysisRunRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/analyze`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    }
  );
}

export function listProjectAppAnalysisRuns(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
): Promise<ProjectAppAnalysisRunRecord[]> {
  return request<ProjectAppAnalysisRunRecord[]>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/analysis-runs`
  );
}

export function getProjectAppAnalysisRun(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  analysisRunId: string,
): Promise<ProjectAppAnalysisRunRecord> {
  return request<ProjectAppAnalysisRunRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/analysis-runs/${encodeURIComponent(analysisRunId)}`
  );
}

export function getProjectAppDeploymentConfig(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  appId: string,
): Promise<ProjectAppDeploymentConfigRecord> {
  return request<ProjectAppDeploymentConfigRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}/deployment-config`
  );
}

export function updateProjectAppDeploymentConfig(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  appId: string,
  payload: ProjectAppDeploymentConfigUpdatePayload,
): Promise<ProjectAppDeploymentConfigRecord> {
  return request<ProjectAppDeploymentConfigRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}/deployment-config`,
    {
      method: "PUT",
      body: JSON.stringify(payload),
    }
  );
}

export function listProjectAppReleases(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  appId: string,
): Promise<ProjectAppDeploymentReleaseRecord[]> {
  return request<ProjectAppDeploymentReleaseRecord[]>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}/deployment-releases`
  );
}

export function createProjectAppRelease(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  appId: string,
  payload: ProjectDeploymentReleaseCreatePayload,
): Promise<ProjectAppDeploymentReleaseRecord> {
  return request<ProjectAppDeploymentReleaseRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}/deployment-releases`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    }
  );
}

export function getProjectAppDeploymentRelease(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  appId: string,
  releaseId: string,
): Promise<ProjectAppDeploymentReleaseRecord> {
  return request<ProjectAppDeploymentReleaseRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}/deployment-releases/${encodeURIComponent(releaseId)}`
  );
}

export function updateProjectAppDeploymentReleaseStatus(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  appId: string,
  releaseId: string,
  payload: ProjectDeploymentReleaseStatusUpdatePayload,
): Promise<ProjectAppDeploymentReleaseRecord> {
  return request<ProjectAppDeploymentReleaseRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}/deployment-releases/${encodeURIComponent(releaseId)}/status`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    }
  );
}

export function applyProjectAppDeploymentResources(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  appId: string,
  payload: ProjectDeploymentApplyResourcesPayload,
): Promise<ProjectDeploymentOperationResultRecord> {
  const resourceKeys = payload.resources
    .map((resource) => normalizeDeploymentItemKey(resource.key))
    .filter((key): key is string => Boolean(key));
  return request<ProjectDeploymentOperationApiRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}/deployment-resources/apply`,
    {
      method: "POST",
      body: JSON.stringify({ resource_keys: resourceKeys }),
    }
  ).then(toDeploymentOperationResult);
}

export function applyProjectAppDeploymentDomains(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  appId: string,
  payload: ProjectDeploymentApplyDomainsPayload,
): Promise<ProjectDeploymentOperationResultRecord> {
  const domainKeys = payload.domains
    .map((domain) => normalizeDeploymentItemKey(domain.key))
    .filter((key): key is string => Boolean(key));
  return request<ProjectDeploymentOperationApiRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}/deployment-domains/apply`,
    {
      method: "POST",
      body: JSON.stringify({ domain_keys: domainKeys }),
    }
  ).then(toDeploymentOperationResult);
}

export function applyProjectAppDeploymentBackups(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  appId: string,
  payload: ProjectDeploymentApplyBackupsPayload,
): Promise<ProjectDeploymentOperationResultRecord> {
  const backupKeys = payload.backup_policies
    .map((policy) => normalizeDeploymentItemKey(policy.key))
    .filter((key): key is string => Boolean(key));
  return request<ProjectDeploymentOperationApiRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}/deployment-backups/apply`,
    {
      method: "POST",
      body: JSON.stringify({ backup_keys: backupKeys }),
    }
  ).then(toDeploymentOperationResult);
}

export function requestProjectAppDeploymentBackupNow(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  appId: string,
  payload: ProjectDeploymentBackupNowPayload,
): Promise<ProjectDeploymentOperationResultRecord> {
  const normalizedBackupKey = normalizeDeploymentItemKey(payload.backup_policy_key);
  const backupKeys = normalizedBackupKey ? [normalizedBackupKey] : [];
  return request<ProjectDeploymentOperationApiRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}/deployment-backups/trigger`,
    {
      method: "POST",
      body: JSON.stringify({ backup_keys: backupKeys }),
    }
  ).then(toDeploymentOperationResult);
}

export function listProjectAppDeploymentBackupExecutions(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  appId: string,
  backupKey: string,
): Promise<ProjectDeploymentBackupExecutionListRecord> {
  const normalizedBackupKey = normalizeDeploymentItemKey(backupKey);
  const query = normalizedBackupKey ? `?backup_key=${encodeURIComponent(normalizedBackupKey)}` : "";
  return request<ProjectDeploymentBackupExecutionListRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}/deployment-backups/executions${query}`
  );
}

export function requestProjectAppDeploymentRestore(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  appId: string,
  payload: ProjectDeploymentRestoreRequestPayload,
): Promise<ProjectDeploymentRestoreRunRecord> {
  return request<ProjectDeploymentRestoreRunRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}/deployment-backups/restore`,
    {
      method: "POST",
      body: JSON.stringify({
        backup_key: payload.backup_key,
        resource_key: payload.resource_key,
        backup_uuid: payload.backup_uuid ?? null,
        execution_uuid: payload.execution_uuid,
        confirmation_value: payload.confirmation_value,
      }),
    }
  );
}

export function listProjectAppDeploymentRestoreRuns(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  appId: string,
): Promise<ProjectDeploymentRestoreRunRecord[]> {
  return request<ProjectDeploymentRestoreRunRecord[]>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}/deployment-backups/restore-runs`
  );
}

export function getProjectAppDeploymentRestoreRun(
  credentials: Credentials,
  tenantId: string,
  projectId: string,
  appId: string,
  restoreRunId: string,
): Promise<ProjectDeploymentRestoreRunRecord> {
  return request<ProjectDeploymentRestoreRunRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/apps/${encodeURIComponent(appId)}/deployment-backups/restore-runs/${encodeURIComponent(restoreRunId)}`
  );
}

export function getTenantDeploymentsOverview(
  credentials: Credentials,
  tenantId: string,
): Promise<TenantDeploymentsOverviewRecord> {
  return request<TenantDeploymentsOverviewRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/deployments/overview`
  );
}

export function getTenantDeploymentPlane(
  credentials: Credentials,
  tenantId: string,
): Promise<TenantDeploymentPlaneRecord> {
  return request<TenantDeploymentPlaneRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/deployment-plane`
  );
}

export function updateTenantDeploymentPlane(
  credentials: Credentials,
  tenantId: string,
  payload: TenantDeploymentPlaneUpdatePayload,
): Promise<TenantDeploymentPlaneRecord> {
  return request<TenantDeploymentPlaneRecord>(
    credentials,
    `/api/admin/tenants/${encodeURIComponent(tenantId)}/deployment-plane`,
    {
      method: "PUT",
      body: JSON.stringify(payload),
    }
  );
}

export function listDeploymentHosts(credentials: Credentials): Promise<DeploymentHostRecord[]> {
  return request<DeploymentHostRecord[]>(
    credentials,
    "/api/admin/deployment-hosts"
  );
}

export function getDeploymentHost(credentials: Credentials, hostId: string): Promise<DeploymentHostRecord> {
  return request<DeploymentHostRecord>(credentials, `/api/admin/deployment-hosts/${encodeURIComponent(hostId)}`);
}

export function createDeploymentHost(
  credentials: Credentials,
  payload: DeploymentHostCreatePayload,
): Promise<DeploymentHostBootstrapRecord> {
  return request<DeploymentHostBootstrapRecord>(
    credentials,
    "/api/admin/deployment-hosts",
    {
      method: "POST",
      body: JSON.stringify({
        label: payload.label,
        provider: payload.provider ?? "internal_coolify",
        infrastructure_provider: payload.infrastructure_provider || null,
        region: payload.region ?? null,
        capabilities: payload.capabilities,
      }),
    }
  );
}

function normalizeDeploymentItemKey(value: string | null | undefined): string | null {
  const normalized = String(value || "").trim().toLowerCase();
  return normalized.length > 0 ? normalized : null;
}

function toDeploymentOperationResult(payload: ProjectDeploymentOperationApiRecord): ProjectDeploymentOperationResultRecord {
  const failed = Number(payload.failed_count || 0);
  const unsupported = Number(payload.unsupported_count || 0);
  const skipped = Number(payload.skipped_count || 0);
  const applied = Number(payload.applied_count || 0);
  const total = Array.isArray(payload.items) ? payload.items.length : 0;
  const detailParts = [`applied ${applied}`];
  if (skipped > 0) {
    detailParts.push(`skipped ${skipped}`);
  }
  if (unsupported > 0) {
    detailParts.push(`unsupported ${unsupported}`);
  }
  if (failed > 0) {
    detailParts.push(`failed ${failed}`);
  }
  const detailSummary = total > 0 ? `${detailParts.join(", ")} (${total} item${total === 1 ? "" : "s"})` : "No items processed.";
  const status = failed > 0 ? "failed" : unsupported > 0 || skipped > 0 ? "partial" : "applied";
  return {
    ok: failed === 0,
    action: payload.operation,
    details: detailSummary,
    status,
    metadata: {
      provider: payload.provider,
      application_uuid: payload.application_uuid ?? null,
      applied_count: applied,
      skipped_count: skipped,
      unsupported_count: unsupported,
      failed_count: failed,
      executed_at: payload.executed_at,
      items: payload.items,
    },
  };
}

