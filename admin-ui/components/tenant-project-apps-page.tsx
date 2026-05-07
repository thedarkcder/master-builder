"use client";

import { useEffect, useMemo, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import {
  applyProjectAppDeploymentBackups,
  applyProjectAppDeploymentDomains,
  applyProjectAppDeploymentResources,
  createProjectAppAnalysisRun,
  createProjectAppRelease,
  getProjectAppDeploymentConfig,
  listProjectAppAnalysisRuns,
  listProjectAppDeploymentBackupExecutions,
  listProjectAppDeploymentRestoreRuns,
  listProjectAppReleases,
  listProjectApps,
  requestProjectAppDeploymentBackupNow,
  requestProjectAppDeploymentRestore,
  updateProjectAppDeploymentConfig,
  type Credentials,
  type ProjectAppAnalysisRunRecord,
  type ProjectAppDeploymentConfigRecord,
  type ProjectAppDeploymentReleaseRecord,
  type ProjectAppRecord,
  type ProjectDeploymentBackupExecutionListRecord,
  type ProjectDeploymentBackupExecutionRecord,
  type ProjectDeploymentApplyBackupsPayload,
  type ProjectDeploymentApplyDomainsPayload,
  type ProjectDeploymentApplyResourcesPayload,
  type ProjectDeploymentBackupNowPayload,
  type ProjectDeploymentReleaseCreatePayload,
  type ProjectDeploymentRestoreRequestPayload,
  type ProjectDeploymentRestoreRunRecord,
  type ProjectDeploymentOperationResultRecord,
  type ProjectDeploymentBackupPolicyRecord,
  type ProjectDeploymentDomainRecord,
  type ProjectDeploymentResourceRecord,
  type ProjectAppDeploymentConfigUpdatePayload,
} from "@/lib/api/deployments";
import { cn } from "@/lib/utils";
import { ArrowRight, Layers3, Plus, RefreshCw, Rocket, Save, Search, Server, ShieldCheck } from "lucide-react";

type TenantProjectAppsPageProps = {
  tenantId: string;
  projectId: string;
  credentials: Credentials | null;
};

type AppSection = "config" | "resources" | "domains" | "backups" | "releases";

type DeploymentDomainDraft = {
  key: string;
  host: string;
  path: string;
  is_primary: boolean;
  tls_enabled: boolean;
};

type DeploymentResourceDraft = {
  key: string;
  kind: string;
  name: string;
  configText: string;
};

type DeploymentBackupDraft = {
  key: string;
  resource_key: string;
  enabled: boolean;
  schedule: string;
  retention_days: string;
  configText: string;
};

type DeploymentFormState = {
  enabled: boolean;
  environment_name: string;
  source_strategy: "dockerfile" | "docker_compose" | "";
  build_strategy: "dockerfile" | "docker_compose" | "nixpacks" | "";
  exposed_port: string;
  start_command: string;
  healthcheckText: string;
  env_schema_text: string;
  secret_schema_text: string;
  domains: DeploymentDomainDraft[];
  resources: DeploymentResourceDraft[];
  backup_policies: DeploymentBackupDraft[];
};

type ReleaseDraft = {
  git_ref: string;
  commit_sha: string;
  reason: string;
};

type OperationKey = "resources" | "domains" | "backups" | "backupNow";

type OperationFeedback = {
  busy: boolean;
  statusLine: string;
  result: ProjectDeploymentOperationResultRecord | null;
};

type RestoreOperationState = {
  busy: boolean;
  statusLine: string;
};

const RESOURCE_KIND_OPTIONS = [
  { value: "postgres", label: "PostgreSQL database" },
  { value: "mysql", label: "MySQL database" },
  { value: "redis", label: "Redis cache" },
  { value: "s3", label: "Object storage (S3)" },
  { value: "volume", label: "Persistent volume" },
  { value: "custom", label: "Custom resource" },
];

const APP_SECTIONS: { id: AppSection; label: string; description: string }[] = [
  { id: "config", label: "Deploy Config", description: "Operational settings and planner contract." },
  { id: "resources", label: "Resources", description: "Databases, caches, object storage, volumes." },
  { id: "domains", label: "Domains / TLS", description: "Managed hostnames and certificates." },
  { id: "backups", label: "Backups", description: "Schedules, retention, backup, restore." },
  { id: "releases", label: "Releases", description: "Create deploys and inspect history." },
];

function emptyDeploymentForm(): DeploymentFormState {
  return {
    enabled: true,
    environment_name: "",
    source_strategy: "dockerfile",
    build_strategy: "dockerfile",
    exposed_port: "",
    start_command: "",
    healthcheckText: "",
    env_schema_text: "{}",
    secret_schema_text: "{}",
    domains: [],
    resources: [],
    backup_policies: [],
  };
}

function emptyReleaseDraft(): ReleaseDraft {
  return {
    git_ref: "",
    commit_sha: "",
    reason: "",
  };
}

function emptyOperationFeedback(): OperationFeedback {
  return { busy: false, statusLine: "", result: null };
}

function trimToNull(value: string): string | null {
  const normalized = value.trim();
  return normalized.length > 0 ? normalized : null;
}

function safeJsonText(value: unknown): string {
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return "{}";
  }
}

function parseJsonObject(value: string): Record<string, unknown> {
  const normalized = value.trim();
  if (!normalized) {
    return {};
  }
  const parsed = JSON.parse(normalized) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("JSON config must be an object");
  }
  return parsed as Record<string, unknown>;
}

function parseFlexibleHealthcheck(value: string): string | Record<string, unknown> | null {
  const normalized = value.trim();
  if (!normalized) {
    return null;
  }
  try {
    const parsed = JSON.parse(normalized) as unknown;
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    // fall through to plain string
  }
  return normalized;
}

function formatTimestamp(value: string | null): string {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function formatConfidence(value: number | null): string {
  if (value == null || Number.isNaN(value)) {
    return "—";
  }
  const scaled = value > 1 ? value : value * 100;
  return `${Math.round(scaled)}%`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => String(item ?? "").trim()).filter(Boolean)
    : [];
}

function toPositiveInteger(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.trunc(parsed) : null;
}

function extractArtifactPrMetadata(result: Record<string, unknown> | null): {
  prUrl: string | null;
  generatedFiles: string[];
  generatedFileCount: number | null;
} | null {
  const artifactPr = isRecord(result?.artifact_pr) ? result.artifact_pr : null;
  if (!artifactPr) {
    return null;
  }
  const prUrl =
    typeof artifactPr.pr_url === "string"
      ? artifactPr.pr_url
      : typeof artifactPr.prUrl === "string"
        ? artifactPr.prUrl
        : typeof artifactPr.url === "string"
          ? artifactPr.url
          : typeof artifactPr.html_url === "string"
            ? artifactPr.html_url
            : typeof artifactPr.link === "string"
              ? artifactPr.link
              : null;
  const generatedFilesCandidate = asStringArray(artifactPr.generated_files);
  const filesCandidate = asStringArray(artifactPr.files);
  const artifactsCandidate = asStringArray(artifactPr.artifacts);
  const generatedFiles = generatedFilesCandidate.length > 0 ? generatedFilesCandidate : filesCandidate.length > 0 ? filesCandidate : artifactsCandidate;
  const generatedFileCount =
    toPositiveInteger(artifactPr.generated_file_count) ??
    toPositiveInteger(artifactPr.generated_files_count) ??
    toPositiveInteger(artifactPr.files_count) ??
    toPositiveInteger(artifactPr.artifact_count) ??
    (generatedFiles.length > 0 ? generatedFiles.length : null);

  return { prUrl, generatedFiles, generatedFileCount };
}

function readAnalysisRunId(run: ProjectAppAnalysisRunRecord | null): string | null {
  if (!run) {
    return null;
  }
  if (typeof run.analysis_run_id === "string" && run.analysis_run_id.trim()) {
    return run.analysis_run_id;
  }
  const fallback = (run as unknown as Record<string, unknown>).run_id;
  return typeof fallback === "string" && fallback.trim() ? fallback : null;
}

function readAnalysisRunError(run: ProjectAppAnalysisRunRecord | null): string | null {
  if (!run) {
    return null;
  }
  if (typeof run.last_error === "string" && run.last_error.trim()) {
    return run.last_error;
  }
  const fallback = (run as unknown as Record<string, unknown>).error;
  return typeof fallback === "string" && fallback.trim() ? fallback : null;
}

function readAnalysisRunResult(run: ProjectAppAnalysisRunRecord | null): Record<string, unknown> | null {
  if (!run) {
    return null;
  }
  if (isRecord(run.raw_result)) {
    return run.raw_result;
  }
  const fallback = (run as unknown as Record<string, unknown>).result_payload;
  return isRecord(fallback) ? fallback : null;
}

function normalizeKey(value: string): string {
  return value.trim().toLowerCase();
}

function uniqueDraftKey(prefix: string, existingKeys: string[]): string {
  const normalizedPrefix = prefix.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "item";
  let candidate = normalizedPrefix;
  let suffix = 2;
  const taken = new Set(existingKeys.map((item) => normalizeKey(item)).filter(Boolean));
  while (taken.has(candidate)) {
    candidate = `${normalizedPrefix}-${suffix}`;
    suffix += 1;
  }
  return candidate;
}

function isBlankDomainDraft(domain: DeploymentDomainDraft): boolean {
  return !domain.key.trim() && !domain.host.trim() && !domain.path.trim() && !domain.is_primary && domain.tls_enabled;
}

function isBlankResourceDraft(resource: DeploymentResourceDraft): boolean {
  return !resource.key.trim() && !resource.kind.trim() && !resource.name.trim() && !resource.configText.trim();
}

function isBlankBackupDraft(backup: DeploymentBackupDraft): boolean {
  return (
    !backup.key.trim() &&
    !backup.resource_key.trim() &&
    !backup.schedule.trim() &&
    !backup.retention_days.trim() &&
    !backup.configText.trim() &&
    backup.enabled
  );
}

function resourceKindLabel(kind: string): string {
  return RESOURCE_KIND_OPTIONS.find((option) => option.value === kind)?.label ?? kind;
}

function appStatusVariant(status: string): "default" | "secondary" | "outline" | "success" | "warning" | "destructive" | "info" {
  const normalized = status.trim().toLowerCase();
  if (normalized === "live") return "success";
  if (normalized === "deploying") return "warning";
  if (normalized === "ready") return "info";
  if (normalized === "needs_pr_merge") return "warning";
  if (normalized === "failed") return "destructive";
  return "outline";
}

function analysisStatusVariant(status: string): "default" | "secondary" | "outline" | "success" | "warning" | "destructive" | "info" {
  const normalized = status.trim().toLowerCase();
  if (normalized === "completed") return "success";
  if (normalized === "running") return "warning";
  if (normalized === "queued") return "info";
  if (normalized === "failed") return "destructive";
  return "outline";
}

function buildStrategyLabel(strategy: string | null | undefined): string {
  const normalized = String(strategy || "").trim().toLowerCase();
  if (normalized === "docker_compose") return "Docker Compose";
  if (normalized === "dockerfile") return "Dockerfile";
  if (normalized === "nixpacks") return "Nixpacks";
  return normalized || "—";
}

function sourceStrategyLabel(strategy: string | null | undefined): string {
  const normalized = String(strategy || "").trim().toLowerCase();
  if (normalized === "docker_compose") return "Docker Compose";
  if (normalized === "dockerfile") return "Dockerfile";
  return normalized || "—";
}

function toDomainDraft(record: ProjectDeploymentDomainRecord): DeploymentDomainDraft {
  return {
    key: record.key ?? "",
    host: record.host ?? "",
    path: record.path ?? "",
    is_primary: Boolean(record.is_primary),
    tls_enabled: record.tls_enabled !== false,
  };
}

function toResourceDraft(record: ProjectDeploymentResourceRecord): DeploymentResourceDraft {
  return {
    key: record.key ?? "",
    kind: record.kind ?? "",
    name: record.name ?? "",
    configText: safeJsonText(record.config ?? {}),
  };
}

function toBackupDraft(record: ProjectDeploymentBackupPolicyRecord): DeploymentBackupDraft {
  return {
    key: record.key ?? "",
    resource_key: record.resource_key ?? "",
    enabled: record.enabled !== false,
    schedule: record.schedule ?? "",
    retention_days: record.retention_days != null ? String(record.retention_days) : "",
    configText: safeJsonText(record.config ?? {}),
  };
}

function toDeploymentForm(record: ProjectAppDeploymentConfigRecord | null): DeploymentFormState {
  if (!record) {
    return emptyDeploymentForm();
  }
  const sourceStrategy =
    record.source_strategy === "dockerfile" || record.source_strategy === "docker_compose" ? record.source_strategy : "";
  const buildStrategy =
    record.build_strategy === "dockerfile" || record.build_strategy === "docker_compose" || record.build_strategy === "nixpacks"
      ? record.build_strategy
      : "";
  return {
    enabled: record.enabled !== false,
    environment_name: record.environment_name ?? "",
    source_strategy: sourceStrategy,
    build_strategy: buildStrategy,
    exposed_port: record.exposed_port != null ? String(record.exposed_port) : "",
    start_command: record.start_command ?? "",
    healthcheckText: typeof record.healthcheck === "string" ? record.healthcheck : safeJsonText(record.healthcheck ?? {}),
    env_schema_text: safeJsonText(record.env_schema_json ?? {}),
    secret_schema_text: safeJsonText(record.secret_schema_json ?? {}),
    domains: (record.domains ?? []).map((domain) => toDomainDraft(domain)),
    resources: (record.resources ?? []).map((resource) => toResourceDraft(resource)),
    backup_policies: (record.backup_policies ?? []).map((backup) => toBackupDraft(backup)),
  };
}

function buildDeploymentPayload(form: DeploymentFormState): ProjectAppDeploymentConfigUpdatePayload {
  const domains = form.domains
    .filter((domain) => !isBlankDomainDraft(domain))
    .map((domain) => ({
      key: normalizeKey(domain.key),
      host: normalizeKey(domain.host),
      path: trimToNull(domain.path),
      is_primary: domain.is_primary,
      tls_enabled: domain.tls_enabled,
    }));

  const resources = form.resources
    .filter((resource) => !isBlankResourceDraft(resource))
    .map((resource) => ({
      key: normalizeKey(resource.key),
      kind: normalizeKey(resource.kind),
      name: trimToNull(resource.name),
      config: parseJsonObject(resource.configText),
    }));

  const backupPolicies = form.backup_policies
    .filter((backup) => !isBlankBackupDraft(backup))
    .map((backup) => ({
      key: normalizeKey(backup.key),
      resource_key: normalizeKey(backup.resource_key),
      enabled: backup.enabled,
      schedule: trimToNull(backup.schedule),
      retention_days: backup.retention_days.trim() ? Number(backup.retention_days.trim()) : null,
      config: parseJsonObject(backup.configText),
    }));

  return {
    enabled: form.enabled,
    environment_name: trimToNull(form.environment_name),
    source_strategy: form.source_strategy || null,
    build_strategy: form.build_strategy || null,
    exposed_port: trimToNull(form.exposed_port) ? Number(form.exposed_port.trim()) : null,
    start_command: trimToNull(form.start_command),
    healthcheck: parseFlexibleHealthcheck(form.healthcheckText),
    env_schema_json: parseJsonObject(form.env_schema_text),
    secret_schema_json: parseJsonObject(form.secret_schema_text),
    domains,
    resources,
    backup_policies: backupPolicies,
  };
}

function validateDeploymentForm(form: DeploymentFormState): string | null {
  const domains = form.domains.filter((domain) => !isBlankDomainDraft(domain));
  const resources = form.resources.filter((resource) => !isBlankResourceDraft(resource));
  const backups = form.backup_policies.filter((backup) => !isBlankBackupDraft(backup));

  if (!form.build_strategy) {
    return "Select a build strategy.";
  }

  const portText = trimToNull(form.exposed_port);
  if (portText) {
    const parsed = Number(portText);
    if (!Number.isFinite(parsed) || parsed < 1 || parsed > 65535) {
      return "Exposed port must be a number between 1 and 65535.";
    }
  }

  const domainKeys = new Set<string>();
  const domainHosts = new Set<string>();
  for (const domain of domains) {
    const key = normalizeKey(domain.key);
    const host = normalizeKey(domain.host);
    if (!key || !host) {
      return "Each domain needs a key and host.";
    }
    if (domainKeys.has(key)) {
      return `Duplicate domain key: ${key}`;
    }
    if (domainHosts.has(host)) {
      return `Duplicate domain host: ${host}`;
    }
    domainKeys.add(key);
    domainHosts.add(host);
    if (domain.path.trim() && !domain.path.trim().startsWith("/")) {
      return `Domain path must start with / for ${key}`;
    }
  }

  const resourceKeys = new Set<string>();
  for (const resource of resources) {
    const key = normalizeKey(resource.key);
    const kind = normalizeKey(resource.kind);
    if (!key || !kind) {
      return "Each resource needs a key and kind.";
    }
    if (resourceKeys.has(key)) {
      return `Duplicate resource key: ${key}`;
    }
    resourceKeys.add(key);
    try {
      parseJsonObject(resource.configText);
    } catch (error) {
      return `Invalid JSON for resource ${key}: ${(error as Error).message}`;
    }
  }

  const backupKeys = new Set<string>();
  for (const backup of backups) {
    const key = normalizeKey(backup.key);
    const resourceKey = normalizeKey(backup.resource_key);
    if (!key || !resourceKey) {
      return "Each backup policy needs a key and resource reference.";
    }
    if (backupKeys.has(key)) {
      return `Duplicate backup policy key: ${key}`;
    }
    if (!resourceKeys.has(resourceKey)) {
      return `Backup policy ${key} references unknown resource ${resourceKey}`;
    }
    backupKeys.add(key);
    if (backup.retention_days.trim() && !Number.isFinite(Number(backup.retention_days.trim()))) {
      return `Backup policy ${key} retention must be a number`;
    }
    try {
      parseJsonObject(backup.configText);
    } catch (error) {
      return `Invalid JSON for backup policy ${key}: ${(error as Error).message}`;
    }
  }

  try {
    parseJsonObject(form.env_schema_text);
  } catch (error) {
    return `Invalid environment schema JSON: ${(error as Error).message}`;
  }
  try {
    parseJsonObject(form.secret_schema_text);
  } catch (error) {
    return `Invalid secret schema JSON: ${(error as Error).message}`;
  }

  return null;
}

function statusLineVariant(status: string): string {
  const normalized = status.trim().toLowerCase();
  if (normalized === "failed") {
    return "border-destructive/30 bg-destructive/10 text-destructive";
  }
  if (normalized === "running" || normalized === "deploying") {
    return "border-warning/30 bg-warning/10 text-warning";
  }
  if (normalized === "queued") {
    return "border-info/30 bg-info/10 text-info";
  }
  if (normalized === "completed" || normalized === "live" || normalized === "succeeded") {
    return "border-success/30 bg-success/10 text-success";
  }
  return "border-border bg-muted/30 text-muted-foreground";
}

function restoreStatusVariant(status: string): "default" | "secondary" | "outline" | "success" | "warning" | "destructive" | "info" {
  const normalized = status.trim().toLowerCase();
  if (normalized === "succeeded" || normalized === "completed") return "success";
  if (normalized === "running" || normalized === "deploying") return "warning";
  if (normalized === "queued") return "info";
  if (normalized === "failed") return "destructive";
  return "outline";
}

function restoreExecutionLabel(execution: ProjectDeploymentBackupExecutionRecord): string {
  const name = execution.file_name?.trim() || execution.execution_uuid;
  const status = execution.status?.trim() ? ` · ${execution.status}` : "";
  return `${name}${status}`;
}

function restoreRunLabel(run: ProjectDeploymentRestoreRunRecord): string {
  return `${run.backup_policy_key} / ${run.execution_uuid}`;
}

function DeploymentOperationCard({
  title,
  description,
  feedback,
  onRun,
  buttonLabel,
  disabled,
}: {
  title: string;
  description: string;
  feedback: OperationFeedback;
  onRun: () => void;
  buttonLabel: string;
  disabled: boolean;
}) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="text-sm">{title}</CardTitle>
            <CardDescription>{description}</CardDescription>
          </div>
          <Button size="sm" variant="outline" onClick={onRun} disabled={disabled || feedback.busy}>
            {feedback.busy ? <RefreshCw className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <ArrowRight className="mr-1.5 h-3.5 w-3.5" />}
            {buttonLabel}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {feedback.statusLine ? (
          <div className={`rounded-xl border px-3 py-2 text-xs ${statusLineVariant(feedback.result?.status ?? feedback.statusLine)}`}>
            {feedback.statusLine}
          </div>
        ) : null}
        {feedback.result ? (
          <pre className="max-h-44 overflow-auto rounded-xl border bg-muted/10 p-3 text-xs">
            {JSON.stringify(feedback.result.metadata ?? { details: feedback.result.details }, null, 2)}
          </pre>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function TenantProjectAppsPage({
  tenantId,
  projectId,
  credentials,
}: TenantProjectAppsPageProps) {
  const { ready } = useAuth();
  const [apps, setApps] = useState<ProjectAppRecord[]>([]);
  const [analysisRuns, setAnalysisRuns] = useState<ProjectAppAnalysisRunRecord[]>([]);
  const [selectedAppId, setSelectedAppId] = useState<string | null>(null);
  const [selectedAppReleases, setSelectedAppReleases] = useState<ProjectAppDeploymentReleaseRecord[]>([]);
  const [selectedSection, setSelectedSection] = useState<AppSection>("config");
  const [deploymentForm, setDeploymentForm] = useState<DeploymentFormState>(() => emptyDeploymentForm());
  const [releaseDraft, setReleaseDraft] = useState<ReleaseDraft>(() => emptyReleaseDraft());
  const [backupNowPolicyKey, setBackupNowPolicyKey] = useState("");
  const [restoreResourceKey, setRestoreResourceKey] = useState("");
  const [restoreBackupKey, setRestoreBackupKey] = useState("");
  const [restoreExecutionUuid, setRestoreExecutionUuid] = useState("");
  const [restoreConfirmationValue, setRestoreConfirmationValue] = useState("");
  const [restoreExecutions, setRestoreExecutions] = useState<ProjectDeploymentBackupExecutionListRecord | null>(null);
  const [restoreRuns, setRestoreRuns] = useState<ProjectDeploymentRestoreRunRecord[]>([]);
  const [restoreBusy, setRestoreBusy] = useState(false);
  const [restoreStatusLine, setRestoreStatusLine] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadingSurface, setLoadingSurface] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [analysisStatusLine, setAnalysisStatusLine] = useState("");
  const [surfaceStatusLine, setSurfaceStatusLine] = useState("");
  const [analysisBusy, setAnalysisBusy] = useState(false);
  const [savingConfig, setSavingConfig] = useState(false);
  const [deploying, setDeploying] = useState(false);
  const [operationFeedback, setOperationFeedback] = useState<Record<OperationKey, OperationFeedback>>({
    resources: emptyOperationFeedback(),
    domains: emptyOperationFeedback(),
    backups: emptyOperationFeedback(),
    backupNow: emptyOperationFeedback(),
  });

  const selectedApp = useMemo(
    () => apps.find((app) => app.app_id === selectedAppId) ?? null,
    [apps, selectedAppId],
  );
  const appSummary = useMemo(() => {
    return apps.reduce(
      (accumulator, app) => {
        const normalized = String(app.status || "").trim().toLowerCase();
        accumulator.total += 1;
        if (normalized === "live") accumulator.live += 1;
        if (normalized === "ready") accumulator.ready += 1;
        if (normalized === "needs_pr_merge") accumulator.needs_pr_merge += 1;
        if (normalized === "deploying") accumulator.deploying += 1;
        if (normalized === "failed") accumulator.failed += 1;
        if (normalized === "draft") accumulator.draft += 1;
        return accumulator;
      },
      { total: 0, draft: 0, needs_pr_merge: 0, ready: 0, deploying: 0, live: 0, failed: 0 },
    );
  }, [apps]);
  const latestAnalysisRun = useMemo(
    () => [...analysisRuns].sort((left, right) => right.created_at.localeCompare(left.created_at))[0] ?? null,
    [analysisRuns],
  );
  const latestAnalysisRunId = useMemo(() => readAnalysisRunId(latestAnalysisRun), [latestAnalysisRun]);
  const latestAnalysisRunError = useMemo(() => readAnalysisRunError(latestAnalysisRun), [latestAnalysisRun]);
  const latestAnalysisRunResult = useMemo(() => readAnalysisRunResult(latestAnalysisRun), [latestAnalysisRun]);
  const latestArtifactPrMetadata = useMemo(
    () => extractArtifactPrMetadata(latestAnalysisRunResult),
    [latestAnalysisRunResult],
  );
  const appReleases = useMemo(
    () => [...selectedAppReleases].sort((left, right) => right.created_at.localeCompare(left.created_at)),
    [selectedAppReleases],
  );
  const restoreRunList = useMemo(
    () => [...restoreRuns].sort((left, right) => right.created_at.localeCompare(left.created_at)),
    [restoreRuns],
  );
  const latestRestoreRun = restoreRunList[0] ?? null;
  const restoreExecutionList = restoreExecutions;
  const restoreExecutionRows = restoreExecutionList?.executions ?? [];
  const selectedRestorePolicy = useMemo(
    () => deploymentForm.backup_policies.find((policy) => normalizeKey(policy.key) === normalizeKey(restoreBackupKey)) ?? null,
    [deploymentForm.backup_policies, restoreBackupKey],
  );
  const selectedRestoreResource = useMemo(
    () => deploymentForm.resources.find((resource) => normalizeKey(resource.key) === normalizeKey(restoreResourceKey)) ?? null,
    [deploymentForm.resources, restoreResourceKey],
  );
  const selectedRestoreExecution = useMemo(
    () => restoreExecutionRows.find((execution) => normalizeKey(execution.execution_uuid) === normalizeKey(restoreExecutionUuid)) ?? null,
    [restoreExecutionRows, restoreExecutionUuid],
  );
  const restoreConfirmationMatches = Boolean(selectedApp && trimToNull(restoreConfirmationValue) === selectedApp.slug.trim());
  const restoreSelectionIsValid =
    Boolean(selectedApp && selectedRestorePolicy && selectedRestoreResource && selectedRestoreExecution) &&
    Boolean(trimToNull(restoreBackupKey)) &&
    Boolean(trimToNull(restoreResourceKey)) &&
    Boolean(trimToNull(restoreExecutionUuid)) &&
    Boolean(restoreConfirmationMatches) &&
    selectedRestorePolicy?.resource_key.trim() === selectedRestoreResource?.key.trim();

  async function loadApps({ silent = false }: { silent?: boolean } = {}) {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      const [loadedApps, loadedAnalysisRuns] = await Promise.all([
        listProjectApps(credentials, tenantId, projectId),
        listProjectAppAnalysisRuns(credentials, tenantId, projectId),
      ]);
      setApps(loadedApps);
      setAnalysisRuns(loadedAnalysisRuns);
      if (!selectedAppId || !loadedApps.some((app) => app.app_id === selectedAppId)) {
        setSelectedAppId(loadedApps[0]?.app_id ?? null);
      }
      if (!silent) {
        setStatusLine(`Loaded ${loadedApps.length} app${loadedApps.length === 1 ? "" : "s"}.`);
      }
    } catch (error) {
      setStatusLine(`Failed to load project apps: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function loadSelectedAppSurface(appId: string) {
    if (!credentials) {
      return;
    }
    setLoadingSurface(true);
    setSurfaceStatusLine("");
    setRestoreStatusLine("");
    try {
      const [config, releases, restoreRunRows] = await Promise.all([
        getProjectAppDeploymentConfig(credentials, tenantId, projectId, appId),
        listProjectAppReleases(credentials, tenantId, projectId, appId),
        listProjectAppDeploymentRestoreRuns(credentials, tenantId, projectId, appId),
      ]);
      setSelectedAppReleases(releases);
      setRestoreRuns(restoreRunRows);
      setRestoreExecutions(null);
      setRestoreBackupKey("");
      setRestoreResourceKey("");
      setRestoreExecutionUuid("");
      setRestoreConfirmationValue("");
      setDeploymentForm(toDeploymentForm(config));
      setReleaseDraft(emptyReleaseDraft());
    } catch (error) {
      setSurfaceStatusLine(`Unable to load managed deployment settings: ${(error as Error).message}`);
      setSelectedAppReleases([]);
      setRestoreRuns([]);
      setRestoreExecutions(null);
      setDeploymentForm(emptyDeploymentForm());
      setRestoreStatusLine(`Unable to load restore history: ${(error as Error).message}`);
    } finally {
      setLoadingSurface(false);
    }
  }

  async function refreshAll({ silent = false }: { silent?: boolean } = {}) {
    await loadApps({ silent });
    if (selectedAppId) {
      await loadSelectedAppSurface(selectedAppId);
    }
  }

  useEffect(() => {
    if (ready && credentials) {
      void loadApps({ silent: true });
    }
  }, [ready, credentials, tenantId, projectId]);

  useEffect(() => {
    if (selectedAppId) {
      void loadSelectedAppSurface(selectedAppId);
    }
  }, [selectedAppId]);

  useEffect(() => {
    const refreshOnVisibility = () => {
      if (document.visibilityState !== "visible") {
        return;
      }
      if (latestAnalysisRun?.status === "queued" || latestAnalysisRun?.status === "running" || latestRestoreRun?.status === "queued" || latestRestoreRun?.status === "running") {
        void refreshAll({ silent: true });
        void refreshRestoreRuns();
      }
    };
    const refreshOnFocus = () => {
      if (latestAnalysisRun?.status === "queued" || latestAnalysisRun?.status === "running" || latestRestoreRun?.status === "queued" || latestRestoreRun?.status === "running") {
        void refreshAll({ silent: true });
        void refreshRestoreRuns();
      }
    };
    window.addEventListener("focus", refreshOnFocus);
    document.addEventListener("visibilitychange", refreshOnVisibility);
    return () => {
      window.removeEventListener("focus", refreshOnFocus);
      document.removeEventListener("visibilitychange", refreshOnVisibility);
    };
  }, [latestAnalysisRun?.status, latestRestoreRun?.status, selectedAppId, credentials, tenantId, projectId]);

  useEffect(() => {
    const firstResourceKey = deploymentForm.resources.find((resource) => !isBlankResourceDraft(resource))?.key?.trim() ?? "";
    const firstBackupPolicyKey = deploymentForm.backup_policies.find((policy) => !isBlankBackupDraft(policy))?.key?.trim() ?? "";
    if (!backupNowPolicyKey && firstBackupPolicyKey) {
      setBackupNowPolicyKey(firstBackupPolicyKey);
    }
    if (!restoreBackupKey && firstBackupPolicyKey) {
      setRestoreBackupKey(firstBackupPolicyKey);
    }
    if (!restoreResourceKey && firstResourceKey) {
      setRestoreResourceKey(firstResourceKey);
    }
  }, [deploymentForm.resources, deploymentForm.backup_policies, backupNowPolicyKey, restoreBackupKey, restoreResourceKey]);

  useEffect(() => {
    if (!selectedApp) {
      return;
    }
    const selectedPolicy = deploymentForm.backup_policies.find(
      (policy) => normalizeKey(policy.key) === normalizeKey(restoreBackupKey),
    );
    const policyResourceKey = selectedPolicy?.resource_key.trim() ?? "";
    if (policyResourceKey && normalizeKey(policyResourceKey) !== normalizeKey(restoreResourceKey)) {
      setRestoreResourceKey(policyResourceKey);
    }
  }, [deploymentForm.backup_policies, restoreBackupKey, restoreResourceKey, selectedApp]);

  useEffect(() => {
    if (!credentials || !selectedAppId) {
      return;
    }
    const backupKey = trimToNull(restoreBackupKey);
    if (!backupKey) {
      setRestoreExecutions(null);
      setRestoreExecutionUuid("");
      return;
    }
    let cancelled = false;
    setRestoreExecutions(null);
    setRestoreExecutionUuid("");
    void listProjectAppDeploymentBackupExecutions(credentials, tenantId, projectId, selectedAppId, backupKey)
      .then((loaded) => {
        if (cancelled) {
          return;
        }
        setRestoreExecutions(loaded);
      })
      .catch((error) => {
        if (cancelled) {
          return;
        }
        setRestoreExecutions(null);
        setRestoreStatusLine(`Unable to load restore executions: ${(error as Error).message}`);
      });
    return () => {
      cancelled = true;
    };
  }, [credentials, selectedAppId, projectId, tenantId, restoreBackupKey]);

  function updateOperationFeedback(key: OperationKey, updates: Partial<OperationFeedback>) {
    setOperationFeedback((current) => ({
      ...current,
      [key]: {
        ...current[key],
        ...updates,
      },
    }));
  }

  async function runOperation(key: OperationKey, runner: () => Promise<ProjectDeploymentOperationResultRecord>) {
    if (!credentials || !selectedApp) {
      return;
    }
    updateOperationFeedback(key, { busy: true, statusLine: "", result: null });
    try {
      const result = await runner();
      updateOperationFeedback(key, {
        busy: false,
        statusLine: result.details || (result.ok ? "Operation completed." : "Operation finished with warnings."),
        result,
      });
      await refreshAll({ silent: true });
      await loadSelectedAppSurface(selectedApp.app_id);
    } catch (error) {
      updateOperationFeedback(key, {
        busy: false,
        statusLine: `Operation failed: ${(error as Error).message}`,
        result: null,
      });
    }
  }

  async function refreshRestoreRuns({ silent = false }: { silent?: boolean } = {}) {
    if (!credentials || !selectedApp) {
      return;
    }
    try {
      const loaded = await listProjectAppDeploymentRestoreRuns(credentials, tenantId, projectId, selectedApp.app_id);
      setRestoreRuns(loaded);
      if (!silent) {
        setRestoreStatusLine(`Loaded ${loaded.length} restore run${loaded.length === 1 ? "" : "s"}.`);
      }
    } catch (error) {
      if (!silent) {
        setRestoreStatusLine(`Unable to refresh restore runs: ${(error as Error).message}`);
      }
    }
  }

  async function analyzeRepo() {
    if (!credentials) {
      return;
    }
    setAnalysisBusy(true);
    setAnalysisStatusLine("");
    try {
      const created = await createProjectAppAnalysisRun(credentials, tenantId, projectId);
      const createdRunId = readAnalysisRunId(created) ?? "unknown";
      setAnalysisStatusLine(`Queued analysis run ${createdRunId}.`);
      await refreshAll({ silent: true });
    } catch (error) {
      setAnalysisStatusLine(`Analysis failed: ${(error as Error).message}`);
    } finally {
      setAnalysisBusy(false);
    }
  }

  async function saveDeploymentConfig() {
    if (!credentials || !selectedApp) {
      return;
    }
    const validationError = validateDeploymentForm(deploymentForm);
    if (validationError) {
      setSurfaceStatusLine(validationError);
      return;
    }
    setSavingConfig(true);
    try {
      const updated = await updateProjectAppDeploymentConfig(
        credentials,
        tenantId,
        projectId,
        selectedApp.app_id,
        buildDeploymentPayload(deploymentForm),
      );
      setDeploymentForm(toDeploymentForm(updated));
      setSurfaceStatusLine("Managed deployment config saved.");
      await refreshAll({ silent: true });
    } catch (error) {
      setSurfaceStatusLine(`Save failed: ${(error as Error).message}`);
    } finally {
      setSavingConfig(false);
    }
  }

  function appendDomain() {
    setDeploymentForm((current) => ({
      ...current,
      domains: [
        ...current.domains,
        {
          key: uniqueDraftKey("domain", current.domains.map((domain) => domain.key)),
          host: "",
          path: "",
          is_primary: current.domains.length === 0,
          tls_enabled: true,
        },
      ],
    }));
  }

  function appendResource(kind: string) {
    setDeploymentForm((current) => ({
      ...current,
      resources: [
        ...current.resources,
        {
          key: uniqueDraftKey(kind, current.resources.map((resource) => resource.key)),
          kind,
          name: resourceKindLabel(kind),
          configText: "{}",
        },
      ],
    }));
  }

  function appendBackupPolicy() {
    setDeploymentForm((current) => ({
      ...current,
      backup_policies: [
        ...current.backup_policies,
        {
          key: uniqueDraftKey("backup", current.backup_policies.map((policy) => policy.key)),
          resource_key: current.resources[0]?.key ?? "",
          enabled: true,
          schedule: "0 2 * * *",
          retention_days: "7",
          configText: "{}",
        },
      ],
    }));
  }

  async function applyResources() {
    if (!credentials || !selectedApp) {
      return;
    }
    const validationError = validateDeploymentForm(deploymentForm);
    if (validationError) {
      updateOperationFeedback("resources", { busy: false, statusLine: validationError, result: null });
      return;
    }
    const payload = buildDeploymentPayload(deploymentForm);
    await runOperation("resources", () =>
      applyProjectAppDeploymentResources(credentials, tenantId, projectId, selectedApp.app_id, {
        resources: payload.resources,
      } as ProjectDeploymentApplyResourcesPayload),
    );
  }

  async function applyDomains() {
    if (!credentials || !selectedApp) {
      return;
    }
    const validationError = validateDeploymentForm(deploymentForm);
    if (validationError) {
      updateOperationFeedback("domains", { busy: false, statusLine: validationError, result: null });
      return;
    }
    const payload = buildDeploymentPayload(deploymentForm);
    await runOperation("domains", () =>
      applyProjectAppDeploymentDomains(credentials, tenantId, projectId, selectedApp.app_id, {
        domains: payload.domains,
      } as ProjectDeploymentApplyDomainsPayload),
    );
  }

  async function applyBackups() {
    if (!credentials || !selectedApp) {
      return;
    }
    const validationError = validateDeploymentForm(deploymentForm);
    if (validationError) {
      updateOperationFeedback("backups", { busy: false, statusLine: validationError, result: null });
      return;
    }
    const payload = buildDeploymentPayload(deploymentForm);
    await runOperation("backups", () =>
      applyProjectAppDeploymentBackups(credentials, tenantId, projectId, selectedApp.app_id, {
        backup_policies: payload.backup_policies,
      } as ProjectDeploymentApplyBackupsPayload),
    );
  }

  async function backupNow() {
    if (!credentials || !selectedApp) {
      return;
    }
    const policyKey = trimToNull(backupNowPolicyKey);
    if (!policyKey) {
      updateOperationFeedback("backupNow", {
        busy: false,
        statusLine: "Add a backup policy before requesting a managed backup.",
        result: null,
      });
      return;
    }
    await runOperation("backupNow", () =>
      requestProjectAppDeploymentBackupNow(credentials, tenantId, projectId, selectedApp.app_id, {
        backup_policy_key: policyKey,
      } as ProjectDeploymentBackupNowPayload),
    );
  }

  async function requestRestore() {
    if (!credentials || !selectedApp) {
      return;
    }
    const backupKey = trimToNull(restoreBackupKey);
    const resourceKey = trimToNull(restoreResourceKey);
    const executionUuid = trimToNull(restoreExecutionUuid);
    const confirmationValue = trimToNull(restoreConfirmationValue);
    if (!backupKey || !resourceKey || !executionUuid) {
      setRestoreStatusLine("Select a backup policy, resource, and execution before requesting a restore.");
      return;
    }
    if (!confirmationValue || confirmationValue !== selectedApp.slug.trim()) {
      setRestoreStatusLine(`Type the app slug exactly to confirm restore: ${selectedApp.slug}`);
      return;
    }
    if (selectedRestorePolicy && selectedRestorePolicy.resource_key.trim() !== resourceKey) {
      setRestoreStatusLine(`Backup policy ${selectedRestorePolicy.key} targets resource ${selectedRestorePolicy.resource_key}.`);
      return;
    }
    setRestoreBusy(true);
    setRestoreStatusLine("");
    try {
      const payload: ProjectDeploymentRestoreRequestPayload = {
        backup_key: backupKey,
        resource_key: resourceKey,
        execution_uuid: executionUuid,
        confirmation_value: confirmationValue,
        ...(restoreExecutions?.backup_uuid ? { backup_uuid: restoreExecutions.backup_uuid } : {}),
      };
      const created = await requestProjectAppDeploymentRestore(credentials, tenantId, projectId, selectedApp.app_id, payload);
      setRestoreRuns((current) => [created, ...current.filter((run) => run.restore_run_id !== created.restore_run_id)]);
      setRestoreExecutionUuid("");
      setRestoreConfirmationValue("");
      setRestoreStatusLine(`Queued restore run ${created.restore_run_id}.`);
      await refreshRestoreRuns({ silent: true });
    } catch (error) {
      setRestoreStatusLine(`Restore failed: ${(error as Error).message}`);
    } finally {
      setRestoreBusy(false);
    }
  }

  async function createRelease() {
    if (!credentials || !selectedApp) {
      return;
    }
    if (!deploymentForm.enabled) {
      setSurfaceStatusLine("Enable managed deployment before creating a release.");
      return;
    }
    if (!deploymentForm.environment_name.trim()) {
      setSurfaceStatusLine("Set an environment name before creating a release.");
      return;
    }
    if (deploymentForm.source_strategy !== "dockerfile") {
      setSurfaceStatusLine("Release creation is currently wired for Dockerfile deployments.");
      return;
    }
    setDeploying(true);
    try {
      const payload: ProjectDeploymentReleaseCreatePayload = {
        git_ref: trimToNull(releaseDraft.git_ref),
        commit_sha: trimToNull(releaseDraft.commit_sha),
        reason: trimToNull(releaseDraft.reason),
      };
      const created = await createProjectAppRelease(credentials, tenantId, projectId, selectedApp.app_id, payload);
      setSelectedAppReleases((current) => [created, ...current.filter((release) => release.release_id !== created.release_id)]);
      setReleaseDraft(emptyReleaseDraft());
      setSurfaceStatusLine(`Queued release ${created.release_id}.`);
      await refreshAll({ silent: true });
      await loadSelectedAppSurface(selectedApp.app_id);
    } catch (error) {
      setSurfaceStatusLine(`Release failed: ${(error as Error).message}`);
    } finally {
      setDeploying(false);
    }
  }

  const latestAnalysisNeedsMerge = String(selectedApp?.status ?? "").trim().toLowerCase() === "needs_pr_merge";

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Button variant="outline" size="sm" onClick={() => void refreshAll()} disabled={busy || loadingSurface}>
          <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
          Refresh
        </Button>
        <Button size="sm" onClick={() => void analyzeRepo()} disabled={analysisBusy || busy}>
          <Search className="mr-1.5 h-3.5 w-3.5" />
          {analysisBusy ? "Analyzing…" : "Analyze Repo"}
        </Button>
      </div>

      {statusLine ? <div className="rounded-xl border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">{statusLine}</div> : null}
      {analysisStatusLine ? (
        <div className="rounded-xl border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">{analysisStatusLine}</div>
      ) : null}
      {surfaceStatusLine ? (
        <div className="rounded-xl border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">{surfaceStatusLine}</div>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {[
          { label: "Apps", value: appSummary.total, note: "Discovered candidates" },
          { label: "Live", value: appSummary.live, note: "Currently deployed" },
          { label: "Ready", value: appSummary.ready, note: "Deployable now" },
          { label: "Needs PR", value: appSummary.needs_pr_merge, note: "Waiting on generated artifacts" },
          { label: "Failed", value: appSummary.failed, note: "Needs attention" },
        ].map((item) => (
          <Card key={item.label}>
            <CardContent className="px-4 py-3">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">{item.label}</p>
              <p className="mt-1 text-2xl font-semibold">{item.value}</p>
              <p className="mt-1 text-xs text-muted-foreground">{item.note}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
          <div>
            <CardTitle className="text-base">Analysis runs</CardTitle>
            <CardDescription>Manual analyze runs use the connected repo and update app candidates.</CardDescription>
          </div>
          <Badge variant={analysisStatusVariant(latestAnalysisRun?.status ?? "idle")}>{latestAnalysisRun?.status ?? "idle"}</Badge>
        </CardHeader>
        <CardContent className="space-y-3">
          {latestAnalysisRun ? (
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={analysisStatusVariant(latestAnalysisRun.status)}>{latestAnalysisRun.status}</Badge>
                {latestAnalysisRunId ? <Badge variant="outline">{latestAnalysisRunId}</Badge> : null}
                <span className="text-xs text-muted-foreground">{formatTimestamp(latestAnalysisRun.created_at)}</span>
              </div>
              {latestAnalysisRunError ? (
                <div className="rounded-xl border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                  {latestAnalysisRunError}
                </div>
              ) : null}
              {latestAnalysisRunResult ? (
                <pre className="max-h-52 overflow-auto rounded-xl border bg-muted/10 p-3 text-xs">
                  {JSON.stringify(latestAnalysisRunResult, null, 2)}
                </pre>
              ) : null}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No analysis runs yet.</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
          <div>
            <CardTitle className="text-base">Discovered apps</CardTitle>
            <CardDescription>Managed deployment is hidden from end users, but all app wiring is available here.</CardDescription>
          </div>
          <div className="flex items-center gap-2">
            {selectedApp ? (
              <Badge variant={appStatusVariant(selectedApp.status)}>{selectedApp.status}</Badge>
            ) : null}
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {apps.length === 0 ? (
            <div className="px-6 py-12 text-center">
              <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-muted">
                <Layers3 className="h-6 w-6 text-muted-foreground" />
              </div>
              <p className="mt-4 text-sm font-medium">No apps discovered yet</p>
              <p className="mt-1 text-sm text-muted-foreground">
                Click Analyze Repo to scan the repository and create one or more app candidates.
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>App</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Confidence</TableHead>
                  <TableHead>Source path</TableHead>
                  <TableHead>Runtime</TableHead>
                  <TableHead>Build</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {apps.map((app) => {
                  const isSelected = app.app_id === selectedAppId;
                  return (
                    <TableRow
                      key={app.app_id}
                      className={cn("cursor-pointer", isSelected && "bg-muted/40")}
                      onClick={() => setSelectedAppId(app.app_id)}
                    >
                      <TableCell>
                        <div className="space-y-1">
                          <div className="flex items-center gap-2">
                            <span className="font-medium">{app.name}</span>
                            <Badge variant={appStatusVariant(app.status)}>{app.status}</Badge>
                          </div>
                          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                            <span className="font-mono">{app.slug}</span>
                            {app.analysis_source ? <span>via {app.analysis_source}</span> : null}
                          </div>
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant={appStatusVariant(app.status)}>{app.status}</Badge>
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">{formatConfidence(app.detection_confidence)}</TableCell>
                      <TableCell className="max-w-[240px] truncate font-mono text-xs" title={app.source_path}>
                        {app.source_path}
                      </TableCell>
                      <TableCell>
                        <div className="space-y-1 text-xs">
                          <p>{app.detected_runtime ?? "—"}</p>
                          <p className="text-muted-foreground">{app.detected_language ?? "—"}</p>
                        </div>
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">{buildStrategyLabel(app.build_strategy)}</TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {selectedApp ? (
        <Card id={selectedApp.app_id}>
          <CardHeader className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <CardTitle className="text-base">{selectedApp.name}</CardTitle>
                  <Badge variant={appStatusVariant(selectedApp.status)}>{selectedApp.status}</Badge>
                  {latestAnalysisNeedsMerge ? <Badge variant="warning">PR required</Badge> : null}
                </div>
                <CardDescription className="space-y-1">
                  <p className="font-mono text-xs">{selectedApp.source_path}</p>
                  <p>{selectedApp.detected_runtime ?? "Unknown runtime"} · {selectedApp.detected_language ?? "Unknown language"}</p>
                </CardDescription>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button variant="outline" size="sm" onClick={() => void loadSelectedAppSurface(selectedApp.app_id)} disabled={loadingSurface || savingConfig || deploying}>
                  <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${loadingSurface ? "animate-spin" : ""}`} />
                  Refresh app
                </Button>
                <Button size="sm" onClick={() => void saveDeploymentConfig()} disabled={savingConfig || deploying || loadingSurface}>
                  <Save className="mr-1.5 h-3.5 w-3.5" />
                  {savingConfig ? "Saving…" : "Save config"}
                </Button>
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              {[
                { label: "Confidence", value: formatConfidence(selectedApp.detection_confidence) },
                { label: "Build strategy", value: buildStrategyLabel(selectedApp.build_strategy) },
                { label: "Source path", value: selectedApp.source_path },
                { label: "Analysis source", value: selectedApp.analysis_source ?? "—" },
              ].map((item) => (
                <div key={item.label} className="rounded-xl border bg-muted/20 px-4 py-3">
                  <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">{item.label}</p>
                  <p className="mt-1 text-sm font-medium">{item.value}</p>
                </div>
              ))}
            </div>

            <div className="grid gap-2 md:grid-cols-5">
              {APP_SECTIONS.map((section) => (
                <button
                  key={section.id}
                  type="button"
                  onClick={() => setSelectedSection(section.id)}
                  className={cn(
                    "rounded-xl border px-4 py-3 text-left transition-colors",
                    selectedSection === section.id ? "border-primary bg-primary/5" : "border-border bg-background hover:bg-muted/40",
                  )}
                >
                  <p className="text-sm font-medium">{section.label}</p>
                  <p className="mt-1 text-xs text-muted-foreground">{section.description}</p>
                </button>
              ))}
            </div>
          </CardHeader>

          <CardContent className="space-y-4">
            {selectedApp.status === "needs_pr_merge" ? (
              <div className="space-y-3 rounded-xl border border-warning/30 bg-warning/10 px-4 py-3 text-sm text-warning">
                <p>Generated deployment files must be merged before this app can be deployed.</p>
                {latestArtifactPrMetadata ? (
                  <div className="space-y-2 rounded-lg border border-warning/20 bg-background/80 px-3 py-2 text-foreground">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium">Artifact PR metadata</span>
                      {latestArtifactPrMetadata.prUrl ? (
                        <a
                          href={latestArtifactPrMetadata.prUrl}
                          target="_blank"
                          rel="noreferrer"
                          className="text-sm font-medium text-primary underline-offset-4 hover:underline"
                        >
                          Open PR
                        </a>
                      ) : null}
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {latestArtifactPrMetadata.generatedFileCount ?? 0} generated file
                      {latestArtifactPrMetadata.generatedFileCount === 1 ? "" : "s"}
                    </p>
                    {latestArtifactPrMetadata.generatedFiles.length > 0 ? (
                      <p className="text-xs text-muted-foreground">
                        {latestArtifactPrMetadata.generatedFiles.join(", ")}
                      </p>
                    ) : null}
                  </div>
                ) : null}
              </div>
            ) : null}

            {selectedSection === "config" ? (
              <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,0.8fr)]">
                <Card>
                  <CardHeader>
                    <CardTitle className="text-sm">Managed deployment config</CardTitle>
                    <CardDescription>Review and update the app-level deployment shape.</CardDescription>
                  </CardHeader>
                  <CardContent className="grid gap-4 md:grid-cols-2">
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Enabled</label>
                      <div className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          checked={deploymentForm.enabled}
                          onChange={(event) => setDeploymentForm((current) => ({ ...current, enabled: event.target.checked }))}
                          className="h-4 w-4 rounded border-input"
                        />
                        <label className="text-sm">Allow managed deployment</label>
                      </div>
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Environment name</label>
                      <Input
                        value={deploymentForm.environment_name}
                        onChange={(event) => setDeploymentForm((current) => ({ ...current, environment_name: event.target.value }))}
                        placeholder="production"
                        disabled={savingConfig || deploying}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Source strategy</label>
                      <select
                        className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                        value={deploymentForm.source_strategy}
                        onChange={(event) =>
                          setDeploymentForm((current) => ({
                            ...current,
                            source_strategy: event.target.value as DeploymentFormState["source_strategy"],
                          }))
                        }
                        disabled={savingConfig || deploying}
                      >
                        <option value="dockerfile">Dockerfile</option>
                        <option value="docker_compose">Docker Compose</option>
                      </select>
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Build strategy</label>
                      <select
                        className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                        value={deploymentForm.build_strategy}
                        onChange={(event) =>
                          setDeploymentForm((current) => ({
                            ...current,
                            build_strategy: event.target.value as DeploymentFormState["build_strategy"],
                          }))
                        }
                        disabled={savingConfig || deploying}
                      >
                        <option value="dockerfile">Dockerfile</option>
                        <option value="docker_compose">Docker Compose</option>
                        <option value="nixpacks">Nixpacks</option>
                      </select>
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Exposed port</label>
                      <Input
                        value={deploymentForm.exposed_port}
                        onChange={(event) => setDeploymentForm((current) => ({ ...current, exposed_port: event.target.value }))}
                        placeholder="3000"
                        disabled={savingConfig || deploying}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Start command</label>
                      <Input
                        value={deploymentForm.start_command}
                        onChange={(event) => setDeploymentForm((current) => ({ ...current, start_command: event.target.value }))}
                        placeholder="npm start"
                        disabled={savingConfig || deploying}
                      />
                    </div>
                    <div className="space-y-1.5 md:col-span-2">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Healthcheck</label>
                      <Textarea
                        value={deploymentForm.healthcheckText}
                        onChange={(event) => setDeploymentForm((current) => ({ ...current, healthcheckText: event.target.value }))}
                        placeholder='{"path": "/health"}'
                        className="min-h-[84px]"
                        disabled={savingConfig || deploying}
                      />
                    </div>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle className="text-sm">Contracts</CardTitle>
                    <CardDescription>Planner output for environment variables and secret refs.</CardDescription>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Environment schema</label>
                      <Textarea
                        value={deploymentForm.env_schema_text}
                        onChange={(event) => setDeploymentForm((current) => ({ ...current, env_schema_text: event.target.value }))}
                        className="min-h-[160px] font-mono text-xs"
                        disabled={savingConfig || deploying}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Secret schema</label>
                      <Textarea
                        value={deploymentForm.secret_schema_text}
                        onChange={(event) => setDeploymentForm((current) => ({ ...current, secret_schema_text: event.target.value }))}
                        className="min-h-[160px] font-mono text-xs"
                        disabled={savingConfig || deploying}
                      />
                    </div>
                    <div className="space-y-2 rounded-xl border bg-muted/20 p-3">
                      <div className="flex items-center gap-2">
                        <ShieldCheck className="h-4 w-4 text-primary" />
                        <p className="text-sm font-medium">Planner metadata</p>
                      </div>
                      <p className="text-xs text-muted-foreground">
                        Runtime: {selectedApp.detected_runtime ?? "—"} · Language: {selectedApp.detected_language ?? "—"}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        Build strategy: {buildStrategyLabel(selectedApp.build_strategy)} · Confidence: {formatConfidence(selectedApp.detection_confidence)}
                      </p>
                    </div>
                  </CardContent>
                </Card>
              </div>
            ) : null}

            {selectedSection === "resources" ? (
              <Card>
                <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
                  <div>
                    <CardTitle className="text-sm">Resources</CardTitle>
                    <CardDescription>Databases, caches, object storage, and volumes.</CardDescription>
                  </div>
                  <div className="flex items-center gap-2">
                    <Button variant="outline" size="sm" onClick={() => appendResource("postgres")} disabled={savingConfig || deploying}>
                      <Plus className="mr-1.5 h-3.5 w-3.5" />
                      Add resource
                    </Button>
                    <Button size="sm" onClick={() => void applyResources()} disabled={savingConfig || deploying}>
                      Apply resources
                    </Button>
                  </div>
                </CardHeader>
                <CardContent className="space-y-3">
                  {deploymentForm.resources.length === 0 ? (
                    <p className="rounded-xl border border-dashed px-4 py-6 text-sm text-muted-foreground">No resources configured yet.</p>
                  ) : (
                    deploymentForm.resources.map((resource, index) => (
                      <div key={`${resource.key || "resource"}-${index}`} className="grid gap-3 rounded-xl border p-4 xl:grid-cols-[1fr_1fr_1.2fr_1.4fr]">
                        <div className="space-y-1.5">
                          <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Key</label>
                          <Input value={resource.key} onChange={(event) => setDeploymentForm((current) => ({
                            ...current,
                            resources: current.resources.map((item, currentIndex) => (currentIndex === index ? { ...item, key: event.target.value } : item)),
                          }))} />
                        </div>
                        <div className="space-y-1.5">
                          <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Kind</label>
                          <select
                            className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                            value={resource.kind}
                            onChange={(event) => setDeploymentForm((current) => ({
                              ...current,
                              resources: current.resources.map((item, currentIndex) => (currentIndex === index ? { ...item, kind: event.target.value } : item)),
                            }))}
                          >
                            {RESOURCE_KIND_OPTIONS.map((option) => (
                              <option key={option.value} value={option.value}>{option.label}</option>
                            ))}
                          </select>
                        </div>
                        <div className="space-y-1.5">
                          <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Name</label>
                          <Input value={resource.name} onChange={(event) => setDeploymentForm((current) => ({
                            ...current,
                            resources: current.resources.map((item, currentIndex) => (currentIndex === index ? { ...item, name: event.target.value } : item)),
                          }))} />
                        </div>
                        <div className="space-y-1.5">
                          <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Config</label>
                          <Textarea
                            value={resource.configText}
                            onChange={(event) => setDeploymentForm((current) => ({
                              ...current,
                              resources: current.resources.map((item, currentIndex) => (currentIndex === index ? { ...item, configText: event.target.value } : item)),
                            }))}
                            className="min-h-[92px] font-mono text-xs"
                          />
                        </div>
                      </div>
                    ))
                  )}
                  {operationFeedback.resources.statusLine ? <p className="text-xs text-muted-foreground">{operationFeedback.resources.statusLine}</p> : null}
                </CardContent>
              </Card>
            ) : null}

            {selectedSection === "domains" ? (
              <Card>
                <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
                  <div>
                    <CardTitle className="text-sm">Domains / TLS</CardTitle>
                    <CardDescription>Managed hostnames and certificate coverage for this app.</CardDescription>
                  </div>
                  <div className="flex items-center gap-2">
                    <Button variant="outline" size="sm" onClick={appendDomain} disabled={savingConfig || deploying}>
                      <Plus className="mr-1.5 h-3.5 w-3.5" />
                      Add domain
                    </Button>
                    <Button size="sm" onClick={() => void applyDomains()} disabled={savingConfig || deploying}>
                      Apply domains
                    </Button>
                  </div>
                </CardHeader>
                <CardContent className="space-y-3">
                  {deploymentForm.domains.length === 0 ? (
                    <p className="rounded-xl border border-dashed px-4 py-6 text-sm text-muted-foreground">No domains configured yet.</p>
                  ) : (
                    deploymentForm.domains.map((domain, index) => (
                      <div key={`${domain.key || "domain"}-${index}`} className="grid gap-3 rounded-xl border p-4 xl:grid-cols-[1fr_1.6fr_1fr_auto]">
                        <div className="space-y-1.5">
                          <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Key</label>
                          <Input
                            value={domain.key}
                            onChange={(event) => setDeploymentForm((current) => ({
                              ...current,
                              domains: current.domains.map((item, currentIndex) => (currentIndex === index ? { ...item, key: event.target.value } : item)),
                            }))}
                          />
                        </div>
                        <div className="space-y-1.5">
                          <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Host</label>
                          <Input
                            value={domain.host}
                            onChange={(event) => setDeploymentForm((current) => ({
                              ...current,
                              domains: current.domains.map((item, currentIndex) => (currentIndex === index ? { ...item, host: event.target.value } : item)),
                            }))}
                            placeholder="app.example.com"
                          />
                        </div>
                        <div className="space-y-1.5">
                          <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Path</label>
                          <Input
                            value={domain.path}
                            onChange={(event) => setDeploymentForm((current) => ({
                              ...current,
                              domains: current.domains.map((item, currentIndex) => (currentIndex === index ? { ...item, path: event.target.value } : item)),
                            }))}
                            placeholder="/"
                          />
                          <div className="flex flex-wrap gap-3 text-sm">
                            <label className="inline-flex items-center gap-2">
                              <input
                                type="checkbox"
                                checked={domain.is_primary}
                                onChange={(event) => setDeploymentForm((current) => ({
                                  ...current,
                                  domains: current.domains.map((item, currentIndex) => (currentIndex === index ? { ...item, is_primary: event.target.checked } : item)),
                                }))}
                                className="h-4 w-4 rounded border-input"
                              />
                              Primary
                            </label>
                            <label className="inline-flex items-center gap-2">
                              <input
                                type="checkbox"
                                checked={domain.tls_enabled}
                                onChange={(event) => setDeploymentForm((current) => ({
                                  ...current,
                                  domains: current.domains.map((item, currentIndex) => (currentIndex === index ? { ...item, tls_enabled: event.target.checked } : item)),
                                }))}
                                className="h-4 w-4 rounded border-input"
                              />
                              TLS
                            </label>
                          </div>
                        </div>
                        <div className="space-y-2 text-xs text-muted-foreground">
                          <p>Managed deployment</p>
                          <p className="font-mono">{domain.key || "domain"}</p>
                        </div>
                      </div>
                    ))
                  )}
                  {operationFeedback.domains.statusLine ? <p className="text-xs text-muted-foreground">{operationFeedback.domains.statusLine}</p> : null}
                </CardContent>
              </Card>
            ) : null}

            {selectedSection === "backups" ? (
              <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,0.8fr)]">
                <Card>
                  <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
                    <div>
                      <CardTitle className="text-sm">Backups</CardTitle>
                      <CardDescription>Schedules and retention for managed deployment resources.</CardDescription>
                    </div>
                    <div className="flex items-center gap-2">
                      <Button variant="outline" size="sm" onClick={appendBackupPolicy} disabled={savingConfig || deploying}>
                        <Plus className="mr-1.5 h-3.5 w-3.5" />
                        Add backup policy
                      </Button>
                      <Button size="sm" onClick={() => void applyBackups()} disabled={savingConfig || deploying}>
                        Apply backups
                      </Button>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    {deploymentForm.backup_policies.length === 0 ? (
                      <p className="rounded-xl border border-dashed px-4 py-6 text-sm text-muted-foreground">No backup policies configured yet.</p>
                    ) : (
                      deploymentForm.backup_policies.map((backup, index) => (
                        <div key={`${backup.key || "backup"}-${index}`} className="grid gap-3 rounded-xl border p-4 xl:grid-cols-[1fr_1fr_1fr_1fr]">
                          <div className="space-y-1.5">
                            <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Key</label>
                            <Input
                              value={backup.key}
                              onChange={(event) => setDeploymentForm((current) => ({
                                ...current,
                                backup_policies: current.backup_policies.map((item, currentIndex) => (currentIndex === index ? { ...item, key: event.target.value } : item)),
                              }))}
                            />
                          </div>
                          <div className="space-y-1.5">
                            <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Resource key</label>
                            <Input
                              value={backup.resource_key}
                              onChange={(event) => setDeploymentForm((current) => ({
                                ...current,
                                backup_policies: current.backup_policies.map((item, currentIndex) => (currentIndex === index ? { ...item, resource_key: event.target.value } : item)),
                              }))}
                              placeholder={deploymentForm.resources[0]?.key ?? "resource"}
                            />
                          </div>
                          <div className="space-y-1.5">
                            <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Schedule</label>
                            <Input
                              value={backup.schedule}
                              onChange={(event) => setDeploymentForm((current) => ({
                                ...current,
                                backup_policies: current.backup_policies.map((item, currentIndex) => (currentIndex === index ? { ...item, schedule: event.target.value } : item)),
                              }))}
                              placeholder="0 2 * * *"
                            />
                          </div>
                          <div className="space-y-1.5">
                            <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Retention days</label>
                            <Input
                              value={backup.retention_days}
                              onChange={(event) => setDeploymentForm((current) => ({
                                ...current,
                                backup_policies: current.backup_policies.map((item, currentIndex) => (currentIndex === index ? { ...item, retention_days: event.target.value } : item)),
                              }))}
                              placeholder="7"
                            />
                          </div>
                        </div>
                      ))
                    )}
                    <div className="grid gap-3 md:grid-cols-2">
                      <div className="space-y-1.5">
                        <label htmlFor="backup-now-policy-select" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Backup now policy</label>
                        <select
                          id="backup-now-policy-select"
                          className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                          value={backupNowPolicyKey}
                          onChange={(event) => setBackupNowPolicyKey(event.target.value)}
                        >
                          <option value="">Select a backup policy</option>
                          {deploymentForm.backup_policies
                            .filter((backup) => !isBlankBackupDraft(backup))
                            .map((backup) => (
                              <option key={backup.key} value={backup.key}>
                                {backup.key} · {backup.resource_key}
                              </option>
                            ))}
                        </select>
                      </div>
                      <div className="space-y-1.5">
                        <label htmlFor="restore-backup-policy-select" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Restore backup policy</label>
                        <select
                          id="restore-backup-policy-select"
                          className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                          value={restoreBackupKey}
                          onChange={(event) => setRestoreBackupKey(event.target.value)}
                        >
                          <option value="">Select a backup policy</option>
                          {deploymentForm.backup_policies
                            .filter((backup) => !isBlankBackupDraft(backup))
                            .map((backup) => (
                              <option key={backup.key} value={backup.key}>
                                {backup.key} · {backup.resource_key}
                              </option>
                            ))}
                        </select>
                      </div>
                      <div className="space-y-1.5">
                        <label htmlFor="restore-resource-select" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Restore resource</label>
                        <select
                          id="restore-resource-select"
                          className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                          value={restoreResourceKey}
                          onChange={(event) => setRestoreResourceKey(event.target.value)}
                        >
                          <option value="">Select a resource</option>
                          {deploymentForm.resources
                            .filter((resource) => !isBlankResourceDraft(resource))
                            .map((resource) => (
                              <option key={resource.key} value={resource.key}>
                                {resource.key} · {resource.kind}
                              </option>
                            ))}
                        </select>
                      </div>
                      <div className="space-y-1.5">
                        <label htmlFor="restore-execution-select" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Restore execution</label>
                        <select
                          id="restore-execution-select"
                          className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                          value={restoreExecutionUuid}
                          onChange={(event) => setRestoreExecutionUuid(event.target.value)}
                          disabled={!trimToNull(restoreBackupKey) || restoreExecutionRows.length === 0}
                        >
                          <option value="">
                            {!trimToNull(restoreBackupKey)
                              ? "Select a backup policy first"
                              : restoreExecutionRows.length === 0
                                ? "No executions available"
                                : "Select an execution"}
                          </option>
                          {restoreExecutionRows.map((execution) => (
                            <option key={execution.execution_uuid} value={execution.execution_uuid}>
                              {restoreExecutionLabel(execution)}
                            </option>
                          ))}
                        </select>
                      </div>
                      <div className="space-y-1.5 md:col-span-2">
                        <label htmlFor="restore-confirmation-input" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                          Confirm restore by typing the app slug
                        </label>
                        <Input
                          id="restore-confirmation-input"
                          value={restoreConfirmationValue}
                          onChange={(event) => setRestoreConfirmationValue(event.target.value)}
                          placeholder={selectedApp?.slug ?? "app-slug"}
                        />
                      </div>
                    </div>
                    <div className="space-y-2 rounded-xl border bg-muted/20 p-4">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div>
                          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Restore execution list</p>
                          <p className="text-sm text-muted-foreground">
                            {restoreExecutions
                              ? `Backup ${restoreExecutions.backup_uuid ?? "—"} for database ${restoreExecutions.database_uuid ?? "—"}`
                              : trimToNull(restoreBackupKey)
                                ? "Loading restore executions..."
                                : "Select a backup policy to load available executions."}
                          </p>
                        </div>
                        <Badge variant="outline">{restoreExecutionRows.length} execution{restoreExecutionRows.length === 1 ? "" : "s"}</Badge>
                      </div>
                      {restoreExecutionRows.length === 0 ? (
                        <div className="rounded-lg border border-dashed px-4 py-6 text-sm text-muted-foreground">
                          {trimToNull(restoreBackupKey) ? "No executions returned for the selected backup policy." : "No backup policy selected yet."}
                        </div>
                      ) : (
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead>Execution</TableHead>
                              <TableHead>Status</TableHead>
                              <TableHead>Created</TableHead>
                              <TableHead>Artifact</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {restoreExecutionRows.map((execution) => (
                              <TableRow key={execution.execution_uuid}>
                                <TableCell className="font-mono text-xs">{execution.execution_uuid}</TableCell>
                                <TableCell>
                                  <Badge variant={restoreStatusVariant(execution.status ?? "")}>{execution.status ?? "unknown"}</Badge>
                                </TableCell>
                                <TableCell className="text-sm text-muted-foreground">{formatTimestamp(execution.created_at)}</TableCell>
                                <TableCell className="max-w-[280px] truncate text-sm text-muted-foreground" title={execution.artifact_path ?? execution.file_name ?? ""}>
                                  {execution.file_name ?? execution.artifact_path ?? "—"}
                                </TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      )}
                    </div>
                    {operationFeedback.backups.statusLine ? <p className="text-xs text-muted-foreground">{operationFeedback.backups.statusLine}</p> : null}
                  </CardContent>
                </Card>

                <div className="space-y-4">
                <DeploymentOperationCard
                    title="Trigger backup"
                    description="Request a managed backup using the configured backup policy."
                    feedback={operationFeedback.backupNow}
                    onRun={() => void backupNow()}
                    buttonLabel="Run backup"
                    disabled={savingConfig || deploying}
                  />
                  <Card>
                    <CardHeader className="pb-3">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <CardTitle className="text-sm">Restore</CardTitle>
                          <CardDescription>Creates an async managed restore run for the selected backup execution.</CardDescription>
                        </div>
                        <Button size="sm" variant="outline" onClick={() => void requestRestore()} disabled={savingConfig || deploying || restoreBusy || !restoreSelectionIsValid}>
                          {restoreBusy ? <RefreshCw className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <ArrowRight className="mr-1.5 h-3.5 w-3.5" />}
                          {restoreBusy ? "Queuing…" : "Run restore"}
                        </Button>
                      </div>
                    </CardHeader>
                    <CardContent className="space-y-3">
                      <div className={`rounded-xl border px-3 py-2 text-xs ${statusLineVariant(restoreBusy ? "running" : latestRestoreRun?.status ?? "queued")}`}>
                        {restoreStatusLine || (latestRestoreRun ? `Latest restore run ${latestRestoreRun.restore_run_id} is ${latestRestoreRun.status}.` : "Choose a backup execution and confirm the app slug to start a restore.")}
                      </div>
                      {latestRestoreRun ? (
                        <div className="grid gap-2 rounded-xl border bg-muted/10 p-3 text-sm">
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-muted-foreground">Latest run</span>
                            <Badge variant={restoreStatusVariant(latestRestoreRun.status)}>{latestRestoreRun.status}</Badge>
                          </div>
                          <div className="font-mono text-xs text-muted-foreground">{latestRestoreRun.restore_run_id}</div>
                          <div className="text-xs text-muted-foreground">{restoreRunLabel(latestRestoreRun)}</div>
                          {latestRestoreRun.last_error ? <p className="text-xs text-destructive">{latestRestoreRun.last_error}</p> : null}
                        </div>
                      ) : null}
                      {restoreRunList.length === 0 ? (
                        <div className="rounded-lg border border-dashed px-4 py-6 text-sm text-muted-foreground">No restore runs recorded for this app yet.</div>
                      ) : (
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead>Run</TableHead>
                              <TableHead>Status</TableHead>
                              <TableHead>Execution</TableHead>
                              <TableHead>Created</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {restoreRunList.map((run) => (
                              <TableRow key={run.restore_run_id}>
                                <TableCell>
                                  <div className="space-y-1">
                                    <p className="font-mono text-xs">{run.restore_run_id}</p>
                                    <p className="text-xs text-muted-foreground">{run.backup_policy_key}</p>
                                  </div>
                                </TableCell>
                                <TableCell>
                                  <Badge variant={restoreStatusVariant(run.status)}>{run.status}</Badge>
                                </TableCell>
                                <TableCell className="max-w-[260px] truncate text-sm text-muted-foreground" title={run.execution_uuid}>
                                  {run.execution_uuid}
                                </TableCell>
                                <TableCell className="text-sm text-muted-foreground">{formatTimestamp(run.created_at)}</TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      )}
                    </CardContent>
                  </Card>
                </div>
              </div>
            ) : null}

            {selectedSection === "releases" ? (
              <div className="grid gap-4 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
                <Card>
                  <CardHeader>
                    <CardTitle className="text-sm">Create release</CardTitle>
                    <CardDescription>Launch a deployment from a branch, tag, or commit.</CardDescription>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Git ref</label>
                      <Input value={releaseDraft.git_ref} onChange={(event) => setReleaseDraft((current) => ({ ...current, git_ref: event.target.value }))} placeholder="main" />
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Commit SHA</label>
                      <Input value={releaseDraft.commit_sha} onChange={(event) => setReleaseDraft((current) => ({ ...current, commit_sha: event.target.value }))} placeholder="abcdef1234" />
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Reason</label>
                      <Textarea value={releaseDraft.reason} onChange={(event) => setReleaseDraft((current) => ({ ...current, reason: event.target.value }))} className="min-h-[92px]" placeholder="Deploy for smoke testing" />
                    </div>
                    <Button size="sm" onClick={() => void createRelease()} disabled={deploying || savingConfig || loadingSurface}>
                      <Rocket className="mr-1.5 h-3.5 w-3.5" />
                      {deploying ? "Queuing…" : "Create release"}
                    </Button>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
                    <div>
                      <CardTitle className="text-sm">Managed release history</CardTitle>
                      <CardDescription>Current releases for this app.</CardDescription>
                    </div>
                    <Badge variant="outline">{appReleases.length} total</Badge>
                  </CardHeader>
                  <CardContent className="p-0">
                    {appReleases.length === 0 ? (
                      <div className="px-6 py-12 text-center text-sm text-muted-foreground">No releases found for this app.</div>
                    ) : (
                      <Table>
                        <TableHeader>
                        <TableRow>
                          <TableHead>Release</TableHead>
                          <TableHead>Status</TableHead>
                          <TableHead>Reference</TableHead>
                          <TableHead>Created</TableHead>
                        </TableRow>
                      </TableHeader>
                        <TableBody>
                          {appReleases.map((release) => (
                            <TableRow key={release.release_id}>
                              <TableCell>
                                <div className="space-y-1">
                                  <p className="font-medium">{release.git_ref || release.commit_sha?.slice(0, 8) || release.release_id}</p>
                                  <p className="font-mono text-xs text-muted-foreground">{release.release_id}</p>
                                </div>
                              </TableCell>
                              <TableCell>
                                <Badge variant={appStatusVariant(release.status)}>{release.status}</Badge>
                              </TableCell>
                              <TableCell className="max-w-[280px] truncate text-sm text-muted-foreground" title={release.deployment_snapshot ? JSON.stringify(release.deployment_snapshot) : ""}>
                                {release.requested_by_user_id ?? release.commit_sha ?? release.git_ref ?? "—"}
                              </TableCell>
                              <TableCell className="text-sm text-muted-foreground">{formatTimestamp(release.created_at)}</TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    )}
                  </CardContent>
                </Card>
              </div>
            ) : null}

            {selectedSection !== "config" ? (
              <div className="grid gap-3 sm:grid-cols-2">
                {operationFeedback.resources.statusLine ? (
                  <div className="rounded-xl border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">{operationFeedback.resources.statusLine}</div>
                ) : null}
                {operationFeedback.domains.statusLine ? (
                  <div className="rounded-xl border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">{operationFeedback.domains.statusLine}</div>
                ) : null}
                {operationFeedback.backups.statusLine ? (
                  <div className="rounded-xl border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">{operationFeedback.backups.statusLine}</div>
                ) : null}
                {operationFeedback.backupNow.statusLine ? (
                  <div className="rounded-xl border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">{operationFeedback.backupNow.statusLine}</div>
                ) : null}
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-12 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted">
              <Server className="h-6 w-6 text-muted-foreground" />
            </div>
            <p className="mt-4 text-sm font-medium">Select an app to review deployment details</p>
            <p className="mt-1 text-sm text-muted-foreground">Run Analyze Repo if you need to create managed deployment candidates.</p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
