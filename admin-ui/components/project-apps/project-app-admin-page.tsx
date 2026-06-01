"use client";

import { type ClipboardEvent, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/components/ui/toast-provider";
import {
  applyProjectAppDeploymentBackups,
  applyProjectAppDeploymentDomains,
  applyProjectAppDeploymentResources,
  applyProjectAppDeploymentVolumes,
  createProjectAppDeploymentRelease,
  deleteProjectApp,
  getProjectApp,
  getProjectAppDeploymentConfig,
  getProjectAppDeploymentReleaseLogs,
  listProjectGitHubBranches,
  listProjectAppAnalysisRuns,
  listProjectAppDeploymentBackupExecutions,
  listProjectAppDeploymentRestoreRuns,
  listProjectAppReleases,
  listProjectApps,
  requestProjectAppDeploymentBackupNow,
  requestProjectAppDeploymentRestore,
  updateProjectApp,
  updateProjectAppDeploymentConfig,
  type Credentials,
  type ProjectAppAnalysisRunRecord,
  type ProjectAppDeploymentConfigRecord,
  type ProjectAppDeploymentReleaseRecord,
  type ProjectAppRecord,
  type ProjectGitHubBranchRecord,
  type ProjectDeploymentReleaseLogsRecord,
  type ProjectDeploymentBackupExecutionListRecord,
  type ProjectDeploymentBackupExecutionRecord,
  type ProjectDeploymentApplyBackupsPayload,
  type ProjectDeploymentApplyDomainsPayload,
  type ProjectDeploymentApplyResourcesPayload,
  type ProjectDeploymentApplyVolumesPayload,
  type ProjectDeploymentBackupNowPayload,
  type ProjectDeploymentReleaseCreatePayload,
  type ProjectDeploymentRestoreRequestPayload,
  type ProjectDeploymentRestoreRunRecord,
  type ProjectDeploymentOperationResultRecord,
  type ProjectDeploymentBackupPolicyRecord,
  type ProjectDeploymentDomainRecord,
  type ProjectDeploymentResourceRecord,
  type ProjectDeploymentServiceRecord,
  type ProjectDeploymentVolumeRecord,
  type ProjectAppDeploymentConfigUpdatePayload,
} from "@/lib/api/deployments";
import { createRunPreview, getRun, type RunRecord } from "@/lib/api/run-events";
import { ArrowLeft, ArrowRight, ExternalLink, Plus, RefreshCw, Save, Server, Trash2 } from "lucide-react";

type ProjectAppAdminPageProps = {
  tenantId: string;
  projectId: string;
  appId: string;
  credentials: Credentials | null;
};

type AppSection = "overview" | "services" | "resources" | "volumes" | "domains" | "backups" | "releases" | "settings" | "environment" | "diagnostics" | "danger";
type DeploymentSubsection = "runtime" | "environment" | "health";
type BackupSubsection = "policies" | "backup-now" | "restore";
type ReleaseSubsection = "history";

type DeploymentDomainDraft = {
  key: string;
  service_key: string;
  host: string;
  path: string;
  tls_enabled: boolean;
};

type DeploymentResourceDraft = {
  key: string;
  kind: string;
  name: string;
  configText: string;
};

type DeploymentServiceDraft = {
  key: string;
  kind: "api" | "website";
  name: string;
  source_path: string;
  compose_service: string;
  build_strategy: string;
  container_port: string;
  public: boolean;
  configText: string;
};

type DeploymentVolumeDraft = {
  key: string;
  type: "persistent" | "file";
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

type EnvironmentVariableDraft = {
  key: string;
  value: string;
};

type SecretReferenceDraft = {
  key: string;
  secret_ref: string;
};

type DeploymentFormState = {
  environment_name: string;
  source_strategy: "dockerfile" | "docker_compose" | "";
  build_strategy: "dockerfile" | "docker_compose" | "nixpacks" | "";
  exposed_port: string;
  start_command: string;
  healthcheckText: string;
  env_schema_text: string;
  secret_schema_text: string;
  environment: EnvironmentVariableDraft[];
  secret_refs: SecretReferenceDraft[];
  domains: DeploymentDomainDraft[];
  services: DeploymentServiceDraft[];
  resources: DeploymentResourceDraft[];
  volumes: DeploymentVolumeDraft[];
  backup_policies: DeploymentBackupDraft[];
};

type OperationKey = "resources" | "volumes" | "domains" | "backups" | "backupNow";

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
  { value: "mariadb", label: "MariaDB database" },
  { value: "redis", label: "Redis cache" },
  { value: "elasticsearch", label: "Elasticsearch" },
  { value: "activemq", label: "ActiveMQ" },
  { value: "kafka", label: "Kafka" },
  { value: "mongodb", label: "MongoDB database" },
  { value: "clickhouse", label: "ClickHouse database" },
  { value: "object_storage", label: "Object storage" },
  { value: "custom", label: "Custom resource" },
];

const VOLUME_TYPE_OPTIONS = [
  { value: "persistent", label: "Persistent volume" },
  { value: "file", label: "File mount" },
] as const;

const SERVICE_BUILD_STRATEGY_OPTIONS = [
  { value: "dockerfile", label: "Dockerfile" },
  { value: "docker_compose", label: "Docker Compose" },
  { value: "maven", label: "Maven" },
  { value: "npm", label: "NPM" },
  { value: "nixpacks", label: "Nixpacks" },
];

const APP_SECTIONS: { id: AppSection; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "services", label: "Services" },
  { id: "releases", label: "Releases" },
  { id: "settings", label: "Settings" },
  { id: "environment", label: "Environment" },
  { id: "domains", label: "Domains" },
  { id: "resources", label: "Resources" },
  { id: "volumes", label: "Volumes" },
  { id: "backups", label: "Backups" },
  { id: "diagnostics", label: "Logs" },
  { id: "danger", label: "Danger" },
];

const DEPLOYMENT_SECTIONS: { id: DeploymentSubsection; label: string }[] = [
  { id: "runtime", label: "Runtime" },
  { id: "health", label: "Health" },
];

const BACKUP_SECTIONS: { id: BackupSubsection; label: string }[] = [
  { id: "policies", label: "Policies" },
  { id: "backup-now", label: "Backup now" },
  { id: "restore", label: "Restore" },
];

const RELEASE_SECTIONS: { id: ReleaseSubsection; label: string }[] = [
  { id: "history", label: "History" },
];

function emptyDeploymentForm(): DeploymentFormState {
  return {
    environment_name: "",
    source_strategy: "dockerfile",
    build_strategy: "dockerfile",
    exposed_port: "",
    start_command: "",
    healthcheckText: "",
    env_schema_text: "{}",
    secret_schema_text: "{}",
    environment: [],
    secret_refs: [],
    domains: [],
    services: [],
    resources: [],
    volumes: [],
    backup_policies: [],
  };
}

function appRuntimeLabel(runtime: string | null | undefined): string {
  const normalized = runtime?.trim();
  return normalized ? normalized : "Auto";
}

function AppAdminSectionTabs({
  selectedSection,
  onSectionChange,
}: {
  selectedSection: AppSection;
  onSectionChange: (section: AppSection) => void;
}) {
  return (
    <div className="overflow-x-auto border-b md:overflow-visible">
      <nav className="-mb-px flex min-w-max gap-1 md:min-w-0 md:flex-wrap" aria-label="Deployment technical controls">
        {APP_SECTIONS.map((section) => {
          const isDanger = section.id === "danger";
          return (
            <button
              key={section.id}
              type="button"
              onClick={() => onSectionChange(section.id)}
              className={[
                "whitespace-nowrap border-b-2 px-4 py-2.5 text-sm font-medium transition-colors",
                selectedSection === section.id
                  ? isDanger
                    ? "border-destructive text-destructive"
                    : "border-primary text-foreground"
                  : isDanger
                    ? "border-transparent text-destructive/70 hover:text-destructive"
                    : "border-transparent text-muted-foreground hover:text-foreground",
              ].join(" ")}
            >
              {section.label}
            </button>
          );
        })}
      </nav>
    </div>
  );
}

function SideNavigation<T extends string>({
  label,
  items,
  selected,
  onSelect,
}: {
  label: string;
  items: { id: T; label: string }[];
  selected: T;
  onSelect: (id: T) => void;
}) {
  return (
    <nav className="space-y-1" aria-label={label}>
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          onClick={() => onSelect(item.id)}
          className={[
            "w-full rounded-xl px-3 py-2 text-left transition-colors",
            selected === item.id ? "bg-primary/10 text-foreground" : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
          ].join(" ")}
        >
          <span className="block text-sm font-medium">{item.label}</span>
        </button>
      ))}
    </nav>
  );
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

function healthcheckToDraft(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (isRecord(value)) {
    const path = value.path;
    if (typeof path === "string") {
      return path;
    }
    const url = value.url;
    if (typeof url === "string") {
      return url;
    }
  }
  return "";
}

function parseDraftConfig(value: string): Record<string, unknown> {
  try {
    return parseJsonObject(value);
  } catch {
    return {};
  }
}

function isSensitiveConfigKey(key: string): boolean {
  return /password|passwd|secret|token|credential|private/i.test(key);
}

function formatConfigValue(key: string, value: unknown): string {
  if (isSensitiveConfigKey(key)) {
    return "Hidden";
  }
  if (value == null) {
    return "—";
  }
  if (Array.isArray(value)) {
    return value.length === 0 ? "None" : `${value.length} ${value.length === 1 ? "item" : "items"}`;
  }
  if (isRecord(value)) {
    const count = Object.keys(value).length;
    return count === 0 ? "None" : `${count} ${count === 1 ? "setting" : "settings"}`;
  }
  if (typeof value === "boolean") {
    return value ? "Yes" : "No";
  }
  return String(value);
}

function ConfigSummary({ configText }: { configText: string }) {
  const entries = Object.entries(parseDraftConfig(configText)).sort(([left], [right]) => left.localeCompare(right));
  if (entries.length === 0) {
    return <p className="text-sm text-muted-foreground">No extra configuration.</p>;
  }
  return (
    <div className="divide-y rounded-xl border bg-muted/10">
      {entries.map(([key, value]) => (
        <div key={key} className="grid gap-1 px-3 py-2 text-sm sm:grid-cols-[minmax(0,0.45fr)_minmax(0,1fr)]">
          <span className="font-medium text-muted-foreground">{key}</span>
          <span className="break-words">{formatConfigValue(key, value)}</span>
        </div>
      ))}
    </div>
  );
}

function OperationResultSummary({ result }: { result: ProjectDeploymentOperationResultRecord }) {
  const rows = [
    ["Action", result.action],
    ["Status", result.status ?? (result.ok ? "completed" : "failed")],
    ["Job", result.job_id ?? null],
    ["Details", result.details],
  ].filter(([, value]) => typeof value === "string" && value.trim().length > 0) as [string, string][];

  return (
    <div className="divide-y rounded-xl border bg-muted/10">
      {rows.map(([label, value]) => (
        <div key={label} className="grid gap-1 px-3 py-2 text-sm sm:grid-cols-[minmax(0,0.25fr)_minmax(0,1fr)]">
          <span className="font-medium text-muted-foreground">{label}</span>
          <span className="break-words">{value}</span>
        </div>
      ))}
    </div>
  );
}

type ServiceUrlGroup = {
  serviceKey: string;
  serviceName: string;
  serviceKind: string;
  urls: ProjectAppDeploymentReleaseRecord["service_urls"];
};

type DeploymentServiceRow = {
  resourceIndex: number;
  key: string;
  name: string;
  serviceType: string;
  public: boolean;
  composeService: string;
  sourcePath: string | null;
  buildStrategy: string;
  containerPort: string | null;
  urls: ProjectAppDeploymentReleaseRecord["service_urls"];
};

function groupServiceUrls(urls: ProjectAppDeploymentReleaseRecord["service_urls"]): ServiceUrlGroup[] {
  const groups = new Map<string, ServiceUrlGroup>();
  for (const serviceUrl of urls) {
    const key = serviceUrl.service_key;
    const existing = groups.get(key);
    if (existing) {
      existing.urls.push(serviceUrl);
      continue;
    }
    groups.set(key, {
      serviceKey: key,
      serviceName: serviceUrl.service_name || key,
      serviceKind: serviceUrl.service_kind,
      urls: [serviceUrl],
    });
  }
  return [...groups.values()].sort((left, right) => left.serviceKey.localeCompare(right.serviceKey));
}

function serviceUrlMatchesResource(
  serviceUrl: ProjectAppDeploymentReleaseRecord["service_urls"][number],
  resourceKey: string,
  composeService: string,
): boolean {
  return normalizeKey(serviceUrl.service_key) === normalizeKey(composeService) || normalizeKey(serviceUrl.service_key) === normalizeKey(resourceKey);
}

function deploymentServiceRows(
  services: DeploymentServiceDraft[],
  serviceUrls: ProjectAppDeploymentReleaseRecord["service_urls"],
): DeploymentServiceRow[] {
  const rows: DeploymentServiceRow[] = [];
  services.forEach((service, serviceIndex) => {
    const serviceType = service.kind;
    const composeService = service.compose_service.trim() || service.key;
    rows.push({
      resourceIndex: serviceIndex,
      key: service.key,
      name: service.name.trim() || service.key,
      serviceType,
      public: service.public,
      composeService,
      sourcePath: service.source_path.trim() || null,
      buildStrategy: service.build_strategy.trim(),
      containerPort: service.container_port.trim() || null,
      urls: serviceUrls.filter((serviceUrl) => serviceUrlMatchesResource(serviceUrl, service.key, composeService)),
    });
  });
  return rows.sort((left, right) => left.key.localeCompare(right.key));
}

function isProjectDeployment(app: ProjectAppRecord): boolean {
  return app.source_path.trim() === ".";
}

function objectToDraftEntries(value: Record<string, unknown> | null | undefined): EnvironmentVariableDraft[] {
  return Object.entries(value ?? {})
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, rawValue]) => ({ key, value: String(rawValue ?? "") }));
}

function objectToSecretRefDrafts(value: Record<string, unknown> | null | undefined): SecretReferenceDraft[] {
  return Object.entries(value ?? {})
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, rawValue]) => ({ key, secret_ref: String(rawValue ?? "") }));
}

function draftEntriesToObject(entries: EnvironmentVariableDraft[]): Record<string, string> {
  const output: Record<string, string> = {};
  for (const entry of entries) {
    const key = entry.key.trim();
    const value = entry.value.trim();
    if (key && value) {
      output[key] = value;
    }
  }
  return output;
}

function secretRefDraftsToObject(entries: SecretReferenceDraft[]): Record<string, string> {
  const output: Record<string, string> = {};
  for (const entry of entries) {
    const key = entry.key.trim();
    const value = entry.secret_ref.trim();
    if (key && value) {
      output[key] = value;
    }
  }
  return output;
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

function relativeTimestamp(value: string | null): string {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  const diffMs = Date.now() - parsed.getTime();
  const absMs = Math.abs(diffMs);
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (absMs < minute) return "just now";
  if (absMs < hour) return `${Math.round(absMs / minute)}m ago`;
  if (absMs < day) return `${Math.round(absMs / hour)}h ago`;
  return `${Math.round(absMs / day)}d ago`;
}

function formatDurationBetween(startValue: string | null, endValue: string | null): string {
  if (!startValue || !endValue) {
    return "—";
  }
  const start = new Date(startValue);
  const end = new Date(endValue);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
    return "—";
  }
  const totalSeconds = Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000));
  if (totalSeconds < 60) {
    return `${totalSeconds}s`;
  }
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) {
    return seconds > 0 ? `${minutes}m ${seconds}s` : `${minutes}m`;
  }
  const hours = Math.floor(minutes / 60);
  const remainderMinutes = minutes % 60;
  return remainderMinutes > 0 ? `${hours}h ${remainderMinutes}m` : `${hours}h`;
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
  return !domain.key.trim() && !domain.service_key.trim() && !domain.host.trim() && !domain.path.trim() && domain.tls_enabled;
}

function isBlankResourceDraft(resource: DeploymentResourceDraft): boolean {
  return !resource.key.trim() && !resource.kind.trim() && !resource.name.trim() && !resource.configText.trim();
}

function isBlankServiceDraft(service: DeploymentServiceDraft): boolean {
  return (
    !service.key.trim() &&
    !service.name.trim() &&
    !service.source_path.trim() &&
    !service.compose_service.trim() &&
    !service.build_strategy.trim() &&
    !service.container_port.trim() &&
    !service.configText.trim() &&
    service.kind === "website" &&
    service.public
  );
}

function isBlankVolumeDraft(volume: DeploymentVolumeDraft): boolean {
  return !volume.key.trim() && !volume.name.trim() && !volume.configText.trim() && volume.type === "persistent";
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

function isBlankEnvironmentVariableDraft(variable: EnvironmentVariableDraft): boolean {
  return !variable.key.trim() && !variable.value.trim();
}

function isBlankSecretReferenceDraft(secret: SecretReferenceDraft): boolean {
  return !secret.key.trim() && !secret.secret_ref.trim();
}

function resourceKindLabel(kind: string): string {
  return RESOURCE_KIND_OPTIONS.find((option) => option.value === kind)?.label ?? kind;
}

function appStatusVariant(status: string): "default" | "secondary" | "outline" | "success" | "warning" | "destructive" | "info" {
  const normalized = status.trim().toLowerCase();
  if (normalized === "live") return "success";
  if (normalized === "deploying" || normalized === "route_activating") return "warning";
  if (normalized === "ready") return "info";
  if (normalized === "needs_pr_merge") return "warning";
  if (normalized === "failed") return "destructive";
  return "outline";
}

function releaseDisplayName(release: ProjectAppDeploymentReleaseRecord | null): string | null {
  if (!release) {
    return null;
  }
  return `${release.git_ref} @ ${release.commit_sha.slice(0, 8)}`;
}

function formatProviderLogs(logs: string): string {
  const normalized = logs.replace(/\r\n?/g, "\n");
  if (normalized.includes("\n")) {
    return normalized;
  }
  return normalized.replace(/\\r\\n|\\n|\\r/g, "\n");
}

function releaseBranchLabel(release: ProjectAppDeploymentReleaseRecord): string {
  if (release.release_kind === "run_preview") {
    return "Preview";
  }
  const normalizedRef = release.git_ref.trim().toLowerCase();
  const tail = normalizedRef.split("/").filter(Boolean).at(-1) ?? normalizedRef;
  if (tail === "stage" || tail === "staging" || tail.startsWith("stage-") || tail.startsWith("staging-")) {
    return "Stage";
  }
  if (tail === "main" || tail === "master" || tail.startsWith("main-") || tail.startsWith("master-")) {
    return "Main";
  }
  return "Branch";
}

function releaseSelectorLabel(release: ProjectAppDeploymentReleaseRecord): string {
  if (release.release_kind === "run_preview") {
    const issueKey = release.source_issue_key?.trim().toUpperCase();
    const summary = release.source_issue_summary?.trim();
    if (issueKey && summary) {
      return `Preview: ${issueKey}: ${summary}`;
    }
    if (issueKey) {
      return `Preview: ${issueKey}`;
    }
  }
  return `${releaseBranchLabel(release)}: ${release.git_ref} @ ${release.commit_sha.slice(0, 8)}`;
}

function isRunPreviewRelease(release: ProjectAppDeploymentReleaseRecord): boolean {
  return release.release_kind === "run_preview";
}

function isActivePreviewRelease(release: ProjectAppDeploymentReleaseRecord): boolean {
  if (!isRunPreviewRelease(release) || release.destroyed_at) {
    return false;
  }
  const normalizedStatus = release.status.trim().toLowerCase();
  return !["destroyed", "failed", "rolled_back"].includes(normalizedStatus);
}

function activePreviewReleasesByBranch(releases: ProjectAppDeploymentReleaseRecord[]): ProjectAppDeploymentReleaseRecord[] {
  const byBranch = new Map<string, ProjectAppDeploymentReleaseRecord>();
  const newestFirst = [...releases].sort((left, right) => right.created_at.localeCompare(left.created_at));
  for (const release of newestFirst) {
    if (!isActivePreviewRelease(release)) {
      continue;
    }
    const key = release.git_ref.trim() || release.source_run_id || release.release_id;
    if (!byBranch.has(key)) {
      byBranch.set(key, release);
    }
  }
  return [...byBranch.values()];
}

function appLatestReleaseDisplayName(app: ProjectAppRecord): string | null {
  const releaseName = app.latest_release_name?.trim();
  if (releaseName) {
    return releaseName;
  }
  const gitRef = app.latest_release_git_ref?.trim();
  const commitSha = app.latest_release_commit_sha?.trim();
  if (gitRef && commitSha) {
    return `${gitRef} @ ${commitSha.slice(0, 8)}`;
  }
  return null;
}

function uniqueStrings(values: Array<string | null | undefined>): string[] {
  return [...new Set(values.map((value) => value?.trim()).filter((value): value is string => Boolean(value)))];
}

function parseEnvironmentVariablePaste(text: string): EnvironmentVariableDraft[] {
  const trimmedText = text.trim();
  if (!trimmedText.includes("=")) return [];

  const assignmentPattern = /(?:^|[;,.|]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*/g;
  const matches = Array.from(trimmedText.matchAll(assignmentPattern));
  if (matches.length === 0) return [];

  const entries: EnvironmentVariableDraft[] = [];
  for (const [index, match] of matches.entries()) {
    const key = match[1]?.trim();
    if (!key) continue;
    const valueStart = match.index + match[0].length;
    const nextMatch = matches[index + 1];
    const valueEnd = nextMatch?.index ?? trimmedText.length;
    entries.push({ key, value: trimmedText.slice(valueStart, valueEnd).trim() });
  }
  return entries;
}

function deploymentDisplayName(
  app: ProjectAppRecord,
  latestRelease: ProjectAppDeploymentReleaseRecord | null,
  fallbackIndex?: number,
): string {
  return releaseDisplayName(latestRelease) ?? appLatestReleaseDisplayName(app) ?? `Deployment ${fallbackIndex != null ? fallbackIndex + 1 : ""}`.trim();
}

function buildStrategyLabel(strategy: string | null | undefined): string {
  const normalized = String(strategy || "").trim().toLowerCase();
  if (normalized === "docker_compose") return "Docker Compose";
  if (normalized === "dockerfile") return "Dockerfile";
  if (normalized === "nixpacks") return "Nixpacks";
  return normalized || "—";
}

function compactBuildStrategyLabel(strategy: string | null | undefined): string {
  const normalized = String(strategy || "").trim().toLowerCase();
  if (normalized === "docker_compose") return "Compose";
  return buildStrategyLabel(strategy);
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
    service_key: record.service_key ?? "",
    host: record.host ?? "",
    path: record.path ?? "",
    tls_enabled: record.tls_enabled !== false,
  };
}

function resourceKindFromRecord(record: ProjectDeploymentResourceRecord): string {
  const rawKind = normalizeKey(record.kind ?? "");
  const serviceType = normalizeKey(String(record.config?.service_type ?? ""));
  if (rawKind === "service" && ["postgres", "mysql", "redis", "elasticsearch", "activemq", "kafka", "object_storage"].includes(serviceType)) {
    return serviceType;
  }
  return rawKind;
}

function toResourceDraft(record: ProjectDeploymentResourceRecord): DeploymentResourceDraft {
  return {
    key: record.key ?? "",
    kind: resourceKindFromRecord(record),
    name: record.name ?? "",
    configText: safeJsonText(record.config ?? {}),
  };
}

function toServiceDraft(record: ProjectDeploymentServiceRecord): DeploymentServiceDraft {
  return {
    key: record.key ?? "",
    kind: record.kind === "api" ? "api" : "website",
    name: record.name ?? "",
    source_path: record.source_path ?? "",
    compose_service: record.compose_service ?? record.key ?? "",
    build_strategy: record.build_strategy ?? "",
    container_port: record.container_port != null ? String(record.container_port) : "",
    public: record.public !== false,
    configText: safeJsonText(record.config ?? {}),
  };
}

function toVolumeDraft(record: ProjectDeploymentVolumeRecord): DeploymentVolumeDraft {
  return {
    key: record.key ?? "",
    type: record.type === "file" ? "file" : "persistent",
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

function toDeploymentForm(record: ProjectAppDeploymentConfigRecord | null, app: ProjectAppRecord | null = null): DeploymentFormState {
  if (!record) {
    return emptyDeploymentForm();
  }
  const sourceStrategy =
    record.source_strategy === "dockerfile" || record.source_strategy === "docker_compose" ? record.source_strategy : "";
  const appBuildStrategy = app?.build_strategy ?? record.build_strategy;
  const buildStrategy =
    appBuildStrategy === "dockerfile" || appBuildStrategy === "docker_compose" || appBuildStrategy === "nixpacks"
      ? appBuildStrategy
      : "";
  return {
    environment_name: record.environment_name ?? "",
    source_strategy: sourceStrategy,
    build_strategy: buildStrategy,
    exposed_port: app?.exposed_port != null ? String(app.exposed_port) : "",
    start_command: app?.start_command ?? "",
    healthcheckText: healthcheckToDraft(app?.healthcheck),
    env_schema_text: safeJsonText(app?.env_schema_json ?? {}),
    secret_schema_text: safeJsonText(app?.secret_schema_json ?? {}),
    environment: objectToDraftEntries(record.environment),
    secret_refs: objectToSecretRefDrafts(record.secret_refs),
    domains: (record.domains ?? []).map((domain) => toDomainDraft(domain)),
    services: (record.services ?? []).map((service) => toServiceDraft(service)),
    resources: (record.resources ?? []).map((resource) => toResourceDraft(resource)),
    volumes: (record.volumes ?? []).map((volume) => toVolumeDraft(volume)),
    backup_policies: (record.backup_policies ?? []).map((backup) => toBackupDraft(backup)),
  };
}

function buildDeploymentPayload(form: DeploymentFormState): ProjectAppDeploymentConfigUpdatePayload {
  const domains = form.domains
    .filter((domain) => !isBlankDomainDraft(domain))
    .map((domain) => ({
      key: normalizeKey(domain.key),
      service_key: normalizeKey(domain.service_key),
      host: normalizeKey(domain.host),
      path: trimToNull(domain.path),
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

  const services = form.services
    .filter((service) => !isBlankServiceDraft(service))
    .map((service) => ({
      key: normalizeKey(service.key),
      kind: service.kind,
      name: trimToNull(service.name),
      source_path: trimToNull(service.source_path),
      compose_service: trimToNull(service.compose_service),
      build_strategy: trimToNull(service.build_strategy),
      container_port: trimToNull(service.container_port) ? Number(service.container_port.trim()) : null,
      public: service.public,
      config: parseJsonObject(service.configText),
    }));

  const volumes = form.volumes
    .filter((volume) => !isBlankVolumeDraft(volume))
    .map((volume) => ({
      key: normalizeKey(volume.key),
      type: volume.type,
      name: trimToNull(volume.name),
      config: parseJsonObject(volume.configText),
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
    enabled: true,
    environment_name: trimToNull(form.environment_name),
    source_strategy: form.source_strategy || null,
    environment: draftEntriesToObject(form.environment),
    secret_refs: secretRefDraftsToObject(form.secret_refs),
    domains,
    services,
    resources,
    volumes,
    backup_policies: backupPolicies,
  };
}

function buildAppRuntimePayload(form: DeploymentFormState): Partial<ProjectAppRecord> {
  return {
    build_strategy: form.build_strategy || null,
    exposed_port: trimToNull(form.exposed_port) ? Number(form.exposed_port.trim()) : null,
    start_command: trimToNull(form.start_command),
    healthcheck: parseFlexibleHealthcheck(form.healthcheckText),
    env_schema_json: parseJsonObject(form.env_schema_text),
    secret_schema_json: parseJsonObject(form.secret_schema_text),
  };
}

function validateDeploymentForm(form: DeploymentFormState): string | null {
  const environmentVariables = form.environment.filter((variable) => !isBlankEnvironmentVariableDraft(variable));
  const secretRefs = form.secret_refs.filter((secret) => !isBlankSecretReferenceDraft(secret));
  const domains = form.domains.filter((domain) => !isBlankDomainDraft(domain));
  const services = form.services.filter((service) => !isBlankServiceDraft(service));
  const resources = form.resources.filter((resource) => !isBlankResourceDraft(resource));
  const volumes = form.volumes.filter((volume) => !isBlankVolumeDraft(volume));
  const backups = form.backup_policies.filter((backup) => !isBlankBackupDraft(backup));

  if (!form.build_strategy) {
    return "Select a build strategy.";
  }
  if (!form.environment_name.trim()) {
    return "Environment name is required.";
  }
  const portText = trimToNull(form.exposed_port);
  if (portText) {
    const parsed = Number(portText);
    if (!Number.isFinite(parsed) || parsed < 1 || parsed > 65535) {
      return "Exposed port must be a number between 1 and 65535.";
    }
  }

  const environmentKeys = new Set<string>();
  for (const variable of environmentVariables) {
    const key = variable.key.trim();
    const value = variable.value.trim();
    if (!key || !value) {
      return "Each environment variable needs a name and value.";
    }
    if (environmentKeys.has(key)) {
      return `Duplicate environment variable: ${key}`;
    }
    environmentKeys.add(key);
  }

  const secretKeys = new Set<string>();
  for (const secret of secretRefs) {
    const key = secret.key.trim();
    const secretRef = secret.secret_ref.trim();
    if (!key || !secretRef) {
      return "Each secret needs an environment name and secret reference.";
    }
    if (secretKeys.has(key)) {
      return `Duplicate secret environment name: ${key}`;
    }
    if (environmentKeys.has(key)) {
      return `${key} is defined as both a variable and a secret. Use one source for each environment name.`;
    }
    secretKeys.add(key);
  }

  const domainKeys = new Set<string>();
  const domainHosts = new Set<string>();
  for (const domain of domains) {
    const key = normalizeKey(domain.key);
    const serviceKey = normalizeKey(domain.service_key);
    const host = normalizeKey(domain.host);
    if (!key || !serviceKey || !host) {
      return "Each domain needs a key, service, and host.";
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

  const serviceKeys = new Set<string>();
  for (const service of services) {
    const key = normalizeKey(service.key);
    if (!key) {
      return "Each service needs a key.";
    }
    if (serviceKeys.has(key)) {
      return `Duplicate service key: ${key}`;
    }
    if (resourceKeys.has(key)) {
      return `Service ${key} duplicates a resource key.`;
    }
    serviceKeys.add(key);
    const portText = trimToNull(service.container_port);
    if (portText) {
      const parsed = Number(portText);
      if (!Number.isFinite(parsed) || parsed < 1 || parsed > 65535) {
        return `Service ${key} port must be a number between 1 and 65535.`;
      }
    }
    try {
      parseJsonObject(service.configText);
    } catch (error) {
      return `Invalid JSON for service ${key}: ${(error as Error).message}`;
    }
  }

  const volumeKeys = new Set<string>();
  for (const volume of volumes) {
    const key = normalizeKey(volume.key);
    if (!key) {
      return "Each volume needs a key.";
    }
    if (volumeKeys.has(key)) {
      return `Duplicate volume key: ${key}`;
    }
    if (resourceKeys.has(key)) {
      return `Volume ${key} duplicates a resource key.`;
    }
    if (serviceKeys.has(key)) {
      return `Volume ${key} duplicates a service key.`;
    }
    volumeKeys.add(key);
    try {
      parseJsonObject(volume.configText);
    } catch (error) {
      return `Invalid JSON for volume ${key}: ${(error as Error).message}`;
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
  if (normalized === "running" || normalized === "deploying" || normalized === "route_activating") {
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
  if (normalized === "running" || normalized === "deploying" || normalized === "route_activating") return "warning";
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

export function ProjectAppAdminPage({
  tenantId,
  projectId,
  appId,
  credentials,
}: ProjectAppAdminPageProps) {
  const { ready } = useAuth();
  const { showToast } = useToast();
  const router = useRouter();
  const searchParams = useSearchParams();
  const requestedReleaseId = searchParams.get("release");
  const [apps, setDeployments] = useState<ProjectAppRecord[]>([]);
  const [analysisRuns, setAnalysisRuns] = useState<ProjectAppAnalysisRunRecord[]>([]);
  const [selectedAppId, setSelectedAppId] = useState<string | null>(appId);
  const [selectedAppReleases, setSelectedAppReleases] = useState<ProjectAppDeploymentReleaseRecord[]>([]);
  const [selectedReleaseId, setSelectedReleaseId] = useState<string | null>(null);
  const [selectedReleaseSourceRun, setSelectedReleaseSourceRun] = useState<RunRecord | null>(null);
  const [selectedReleaseLogs, setSelectedReleaseLogs] = useState<ProjectDeploymentReleaseLogsRecord | null>(null);
  const [selectedReleaseLogsLoading, setSelectedReleaseLogsLoading] = useState(false);
  const [selectedReleaseLogsError, setSelectedReleaseLogsError] = useState("");
  const [githubBranches, setGithubBranches] = useState<ProjectGitHubBranchRecord[]>([]);
  const [selectedSection, setSelectedSection] = useState<AppSection>("overview");
  const [selectedDeploymentSection, setSelectedDeploymentSection] = useState<DeploymentSubsection>("runtime");
  const [selectedBackupSection, setSelectedBackupSection] = useState<BackupSubsection>("policies");
  const [selectedReleaseSection, setSelectedReleaseSection] = useState<ReleaseSubsection>("history");
  const [deploymentForm, setDeploymentForm] = useState<DeploymentFormState>(() => emptyDeploymentForm());
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
  const [savingConfig, setSavingConfig] = useState(false);
  const [manualDeployBusy, setManualDeployBusy] = useState(false);
  const [manualDeployGitRef, setManualDeployGitRef] = useState("");
  const [manualDeployCommitSha, setManualDeployCommitSha] = useState("");
  const deploying = false;
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteConfirmationValue, setDeleteConfirmationValue] = useState("");
  const [operationFeedback, setOperationFeedback] = useState<Record<OperationKey, OperationFeedback>>({
    resources: emptyOperationFeedback(),
    volumes: emptyOperationFeedback(),
    domains: emptyOperationFeedback(),
    backups: emptyOperationFeedback(),
    backupNow: emptyOperationFeedback(),
  });

  const selectedApp = useMemo(
    () => apps.find((app) => app.app_id === selectedAppId) ?? null,
    [apps, selectedAppId],
  );
  const latestAnalysisRun = useMemo(
    () => [...analysisRuns].sort((left, right) => right.created_at.localeCompare(left.created_at))[0] ?? null,
    [analysisRuns],
  );
  const latestAnalysisRunResult = useMemo(() => readAnalysisRunResult(latestAnalysisRun), [latestAnalysisRun]);
  const latestArtifactPrMetadata = useMemo(
    () => extractArtifactPrMetadata(latestAnalysisRunResult),
    [latestAnalysisRunResult],
  );
  const appReleases = useMemo(
    () => [...selectedAppReleases].sort((left, right) => right.created_at.localeCompare(left.created_at)),
    [selectedAppReleases],
  );
  const deploymentReleases = useMemo(
    () => appReleases.filter((release) => !isRunPreviewRelease(release)),
    [appReleases],
  );
  const latestProductionRelease = deploymentReleases[0] ?? null;
  const selectedRelease = selectedReleaseId ? appReleases.find((release) => release.release_id === selectedReleaseId) ?? null : null;
  const latestRelease = selectedRelease ?? latestProductionRelease ?? null;
  const latestReleaseIsPreview = latestRelease ? isRunPreviewRelease(latestRelease) : false;
  const activePreviewReleases = useMemo(
    () => activePreviewReleasesByBranch(appReleases),
    [appReleases],
  );
  const latestServiceUrlGroups = useMemo(
    () => groupServiceUrls(latestRelease?.service_urls ?? []),
    [latestRelease],
  );
  const latestServiceUrls = latestRelease?.service_urls ?? [];
  const serviceRows = useMemo(
    () => deploymentServiceRows(deploymentForm.services, latestServiceUrls),
    [deploymentForm.services, latestServiceUrls],
  );
  const infrastructureResourceEntries = useMemo(
    () =>
      deploymentForm.resources
        .map((resource, index) => ({ resource, index }))
        .filter(({ resource }) => resource.kind.trim().toLowerCase() !== "service"),
    [deploymentForm.resources],
  );
  const previewServiceUrl = latestServiceUrls.find((serviceUrl) => serviceUrl.service_kind === "website") ?? latestServiceUrls[0] ?? null;
  const firstVisitUrl = previewServiceUrl?.url ?? null;
  const latestReleaseDuration = formatDurationBetween(latestRelease?.started_at ?? null, latestRelease?.completed_at ?? null);
  const latestReleaseCreatedAt = latestRelease?.created_at ?? null;
  const latestReleaseSource = releaseDisplayName(latestRelease);
  const latestReleaseEnvironment = latestRelease?.environment_name ?? null;
  const selectedDeploymentName = selectedApp ? deploymentDisplayName(selectedApp, latestRelease) : "Deployment";
  const latestReleaseIssueKey = latestRelease?.source_issue_key?.trim().toUpperCase() || selectedReleaseSourceRun?.issue_key?.trim().toUpperCase() || null;
  const latestReleaseIssueSummary = latestRelease?.source_issue_summary?.trim() || selectedReleaseSourceRun?.issue_summary?.trim() || null;
  const latestReleaseIssueUrl = latestRelease?.source_issue_url?.trim() || selectedReleaseSourceRun?.issue_url?.trim() || null;
  const selectedDeploymentTitle = latestReleaseIssueKey
    ? `${latestReleaseIssueKey}${latestReleaseIssueSummary ? `: ${latestReleaseIssueSummary}` : ""}`
    : selectedDeploymentName;
  const selectedReleaseLabel = latestRelease ? releaseBranchLabel(latestRelease) : "Deployment";
  const selectedStatus = latestRelease?.status ?? selectedApp?.status ?? "";
  const manualDeployBranchOptions = useMemo(
    () => uniqueStrings([...githubBranches.map((branch) => branch.name), ...deploymentReleases.map((release) => release.git_ref), selectedApp?.latest_release_git_ref]),
    [deploymentReleases, githubBranches, selectedApp?.latest_release_git_ref],
  );
  const manualDeployCommitOptions = useMemo(
    () =>
      uniqueStrings([
        ...deploymentReleases
          .filter((release) => release.git_ref === manualDeployGitRef)
          .map((release) => release.commit_sha),
        selectedApp?.latest_release_git_ref === manualDeployGitRef ? selectedApp.latest_release_commit_sha : null,
      ]),
    [deploymentReleases, manualDeployGitRef, selectedApp?.latest_release_commit_sha, selectedApp?.latest_release_git_ref],
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

  useEffect(() => {
    const sourceRunId = latestRelease?.source_run_id?.trim();
    if (!credentials || !sourceRunId) {
      setSelectedReleaseSourceRun(null);
      return;
    }
    let cancelled = false;
    void getRun(credentials, sourceRunId)
      .then((run) => {
        if (cancelled) return;
        setSelectedReleaseSourceRun(run);
      })
      .catch(() => {
        if (cancelled) return;
        setSelectedReleaseSourceRun(null);
      });
    return () => {
      cancelled = true;
    };
  }, [credentials, latestRelease?.source_run_id]);

  useEffect(() => {
    if (!credentials) {
      setGithubBranches([]);
      return;
    }
    let cancelled = false;
    void listProjectGitHubBranches(credentials, tenantId, projectId)
      .then((branches) => {
        if (cancelled) return;
        setGithubBranches(branches);
      })
      .catch(() => {
        if (cancelled) return;
        setGithubBranches([]);
      });
    return () => {
      cancelled = true;
    };
  }, [credentials, projectId, tenantId]);

  async function loadSelectedReleaseLogs(release: ProjectAppDeploymentReleaseRecord | null = latestRelease) {
    if (!credentials || !release || !selectedAppId || release.provider !== "internal_coolify") {
      setSelectedReleaseLogs(null);
      setSelectedReleaseLogsError("");
      return;
    }
    setSelectedReleaseLogsLoading(true);
    setSelectedReleaseLogsError("");
    try {
      const logs = await getProjectAppDeploymentReleaseLogs(
        credentials,
        tenantId,
        projectId,
        selectedAppId,
        release.release_id,
      );
      setSelectedReleaseLogs(logs);
    } catch (error) {
      setSelectedReleaseLogs(null);
      setSelectedReleaseLogsError((error as Error).message);
    } finally {
      setSelectedReleaseLogsLoading(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    if (!credentials || !latestRelease || !selectedAppId || latestRelease.provider !== "internal_coolify") {
      setSelectedReleaseLogs(null);
      setSelectedReleaseLogsError("");
      setSelectedReleaseLogsLoading(false);
      return;
    }
    setSelectedReleaseLogsLoading(true);
    setSelectedReleaseLogsError("");
    void getProjectAppDeploymentReleaseLogs(credentials, tenantId, projectId, selectedAppId, latestRelease.release_id)
      .then((logs) => {
        if (cancelled) return;
        setSelectedReleaseLogs(logs);
      })
      .catch((error) => {
        if (cancelled) return;
        setSelectedReleaseLogs(null);
        setSelectedReleaseLogsError((error as Error).message);
      })
      .finally(() => {
        if (cancelled) return;
        setSelectedReleaseLogsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [credentials, latestRelease, projectId, selectedAppId, tenantId]);

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

  async function loadDeployments({ silent = false }: { silent?: boolean } = {}) {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      const [loadedDeployments, loadedAnalysisRuns] = await Promise.all([
        listProjectApps(credentials, tenantId, projectId),
        listProjectAppAnalysisRuns(credentials, tenantId, projectId),
      ]);
      const projectDeployments = loadedDeployments.filter(isProjectDeployment);
      setDeployments(projectDeployments);
      setAnalysisRuns(loadedAnalysisRuns);
      if (projectDeployments.some((app) => app.app_id === appId)) {
        setSelectedAppId(appId);
      } else {
        setSelectedAppId(null);
        setSelectedReleaseId(null);
      }
    } catch (error) {
      if (!silent) {
        showToast({
          title: "Unable to load deployments",
          description: (error as Error).message,
          tone: "error",
        });
      }
    } finally {
      setBusy(false);
    }
  }

  async function loadSelectedAppSurface(appId: string) {
    if (!credentials) {
      return;
    }
    setLoadingSurface(true);
    setRestoreStatusLine("");
    try {
      const [appRecord, config, releases, restoreRunRows] = await Promise.all([
        getProjectApp(credentials, tenantId, projectId, appId),
        getProjectAppDeploymentConfig(credentials, tenantId, projectId, appId),
        listProjectAppReleases(credentials, tenantId, projectId, appId),
        listProjectAppDeploymentRestoreRuns(credentials, tenantId, projectId, appId),
      ]);
      setDeployments((current) => {
        const withoutLoaded = current.filter((app) => app.app_id !== appRecord.app_id);
        return [appRecord, ...withoutLoaded];
      });
      setSelectedAppReleases(releases);
      setSelectedReleaseId((current) => (current && releases.some((release) => release.release_id === current) ? current : null));
      setRestoreRuns(restoreRunRows);
      setRestoreExecutions(null);
      setRestoreBackupKey("");
      setRestoreResourceKey("");
      setRestoreExecutionUuid("");
      setRestoreConfirmationValue("");
      setDeploymentForm(toDeploymentForm(config, appRecord));
    } catch (error) {
      showToast({
        title: "Unable to load deployment settings",
        description: (error as Error).message,
        tone: "error",
      });
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
    await loadDeployments({ silent });
    if (selectedAppId) {
      await loadSelectedAppSurface(selectedAppId);
    }
  }

  useEffect(() => {
    if (ready && credentials) {
      void loadDeployments({ silent: true });
    }
  }, [ready, credentials, tenantId, projectId]);

  useEffect(() => {
    if (selectedAppId) {
      void loadSelectedAppSurface(selectedAppId);
    }
  }, [selectedAppId]);

  useEffect(() => {
    if (!requestedReleaseId) {
      return;
    }
    if (selectedAppReleases.some((release) => release.release_id === requestedReleaseId)) {
      setSelectedReleaseId(requestedReleaseId);
    }
  }, [requestedReleaseId, selectedAppReleases]);

  useEffect(() => {
    const nextGitRef = latestRelease?.git_ref ?? selectedApp?.latest_release_git_ref ?? "";
    const nextCommitSha = latestRelease?.commit_sha ?? selectedApp?.latest_release_commit_sha ?? "";
    setManualDeployGitRef((current) => current || nextGitRef);
    setManualDeployCommitSha((current) => current || nextCommitSha);
  }, [latestRelease?.git_ref, latestRelease?.commit_sha, selectedApp?.latest_release_git_ref, selectedApp?.latest_release_commit_sha]);

  useEffect(() => {
    if (manualDeployBranchOptions.length === 0) {
      return;
    }
    setManualDeployGitRef((current) => (current && manualDeployBranchOptions.includes(current) ? current : manualDeployBranchOptions[0] ?? ""));
  }, [manualDeployBranchOptions]);

  useEffect(() => {
    if (manualDeployCommitOptions.length === 0) {
      setManualDeployCommitSha("");
      return;
    }
    setManualDeployCommitSha((current) => (current && manualDeployCommitOptions.includes(current) ? current : manualDeployCommitOptions[0] ?? ""));
  }, [manualDeployCommitOptions]);

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

  async function saveDeploymentConfig() {
    if (!credentials || !selectedApp) {
      return;
    }
    const validationError = validateDeploymentForm(deploymentForm);
    if (validationError) {
      showToast({
        title: "Deployment settings invalid",
        description: validationError,
        tone: "error",
      });
      return;
    }
    setSavingConfig(true);
    try {
      const updatedApp = await updateProjectApp(
        credentials,
        tenantId,
        projectId,
        selectedApp.app_id,
        buildAppRuntimePayload(deploymentForm),
      );
      const updated = await updateProjectAppDeploymentConfig(
        credentials,
        tenantId,
        projectId,
        selectedApp.app_id,
        buildDeploymentPayload(deploymentForm),
      );
      setDeploymentForm(toDeploymentForm(updated, updatedApp));
      showToast({
        title: "Deployment settings saved",
        tone: "success",
      });
      await refreshAll({ silent: true });
    } catch (error) {
      showToast({
        title: "Save failed",
        description: (error as Error).message,
        tone: "error",
      });
    } finally {
      setSavingConfig(false);
    }
  }

  function updateServiceBuildStrategy(resourceIndex: number, buildStrategy: string) {
    setDeploymentForm((current) => ({
      ...current,
      services: current.services.map((service, currentIndex) => {
        if (currentIndex !== resourceIndex) {
          return service;
        }
        return {
          ...service,
          build_strategy: buildStrategy,
        };
      }),
    }));
  }

  async function triggerManualDeploy() {
    if (!credentials || !selectedApp) {
      return;
    }
    const gitRef = trimToNull(manualDeployGitRef);
    const commitSha = trimToNull(manualDeployCommitSha);
    if (!gitRef || !commitSha) {
      showToast({
        title: "Manual deploy needs a branch and commit",
        tone: "error",
      });
      return;
    }
    const payload: ProjectDeploymentReleaseCreatePayload = {
      git_ref: gitRef,
      commit_sha: commitSha,
      reason: "Manual deploy",
    };
    setManualDeployBusy(true);
    try {
      const release = await createProjectAppDeploymentRelease(
        credentials,
        tenantId,
        projectId,
        selectedApp.app_id,
        payload,
      );
      setSelectedAppReleases((current) => [release, ...current.filter((item) => item.release_id !== release.release_id)]);
      showToast({
        title: "Deployment started",
        tone: "success",
      });
      await refreshAll({ silent: true });
      await loadSelectedAppSurface(selectedApp.app_id);
    } catch (error) {
      showToast({
        title: "Manual deploy failed",
        description: (error as Error).message,
        tone: "error",
      });
    } finally {
      setManualDeployBusy(false);
    }
  }

  async function triggerPreviewDeploy() {
    if (!credentials || !selectedApp || !latestRelease) {
      return;
    }
    const sourceRunId = latestRelease.source_run_id?.trim();
    if (!latestReleaseIsPreview || !sourceRunId) {
      showToast({
        title: "Preview needs a source run",
        tone: "error",
      });
      return;
    }
    setManualDeployBusy(true);
    try {
      const release = await createRunPreview(credentials, sourceRunId, { force: true });
      const appRelease: ProjectAppDeploymentReleaseRecord = {
        ...release,
        app_id: release.app_id ?? selectedApp.app_id,
      };
      setSelectedAppReleases((current) => [appRelease, ...current.filter((item) => item.release_id !== appRelease.release_id)]);
      setSelectedReleaseId(appRelease.release_id);
      router.push(`${deploymentsHref}/${encodeURIComponent(selectedApp.app_id)}?release=${encodeURIComponent(appRelease.release_id)}`);
      showToast({
        title: "Preview started",
        tone: "success",
      });
      await refreshAll({ silent: true });
      await loadSelectedAppSurface(selectedApp.app_id);
    } catch (error) {
      showToast({
        title: "Preview failed",
        description: (error as Error).message,
        tone: "error",
      });
    } finally {
      setManualDeployBusy(false);
    }
  }

  async function triggerReleaseAction() {
    if (latestReleaseIsPreview) {
      await triggerPreviewDeploy();
      return;
    }
    await triggerManualDeploy();
  }

  function appendDomain() {
    setDeploymentForm((current) => ({
      ...current,
      domains: [
        ...current.domains,
        {
          key: uniqueDraftKey("domain", current.domains.map((domain) => domain.key)),
          service_key: "",
          host: "",
          path: "",
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

  function appendVolume(type: "persistent" | "file") {
    setDeploymentForm((current) => ({
      ...current,
      volumes: [
        ...current.volumes,
        {
          key: uniqueDraftKey("volume", current.volumes.map((volume) => volume.key)),
          type,
          name: "Persistent volume",
          configText: safeJsonText({ mount_path: "/data" }),
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

  function appendEnvironmentVariable() {
    setDeploymentForm((current) => ({
      ...current,
      environment: [...current.environment, { key: "", value: "" }],
    }));
  }

  function handleEnvironmentVariablePaste(event: ClipboardEvent<HTMLInputElement>, index: number) {
    const pastedText = event.clipboardData.getData("text/plain") || event.clipboardData.getData("text");
    const entries = parseEnvironmentVariablePaste(pastedText);
    if (entries.length === 0) return;
    event.preventDefault();
    setDeploymentForm((current) => ({
      ...current,
      environment: [
        ...current.environment.slice(0, index),
        ...entries,
        ...current.environment.slice(index + 1),
      ],
    }));
  }

  function appendSecretReference() {
    setDeploymentForm((current) => ({
      ...current,
      secret_refs: [...current.secret_refs, { key: "", secret_ref: "" }],
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

  async function applyVolumes() {
    if (!credentials || !selectedApp) {
      return;
    }
    const validationError = validateDeploymentForm(deploymentForm);
    if (validationError) {
      updateOperationFeedback("volumes", { busy: false, statusLine: validationError, result: null });
      return;
    }
    const payload = buildDeploymentPayload(deploymentForm);
    await runOperation("volumes", () =>
      applyProjectAppDeploymentVolumes(credentials, tenantId, projectId, selectedApp.app_id, {
        volumes: payload.volumes,
      } as ProjectDeploymentApplyVolumesPayload),
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
      setRestoreStatusLine(`Type the deployment slug exactly to confirm restore: ${selectedApp.slug}`);
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

  async function removeSelectedApp() {
    if (!credentials || !selectedApp) {
      return;
    }
    if (deleteConfirmationValue.trim() !== selectedApp.slug.trim()) {
      showToast({
        title: "Confirmation does not match",
        description: `Type the deployment slug to remove it.`,
        tone: "error",
      });
      return;
    }
    setDeleteBusy(true);
    try {
      await deleteProjectApp(credentials, tenantId, projectId, selectedApp.app_id);
      showToast({
        title: "Deployment removed",
        description: `${selectedDeploymentName} was removed from this project.`,
        tone: "success",
      });
      router.push(deploymentsHref);
    } catch (error) {
      showToast({
        title: "Remove failed",
        description: (error as Error).message,
        tone: "error",
      });
    } finally {
      setDeleteBusy(false);
    }
  }

  const latestAnalysisNeedsMerge = String(selectedApp?.status ?? "").trim().toLowerCase() === "needs_pr_merge";
  const deploymentsHref = `/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/deployments`;
  const deleteConfirmationMatches = Boolean(selectedApp && deleteConfirmationValue.trim() === selectedApp.slug.trim());
  const releaseLogsContent = latestRelease?.provider === "internal_coolify" ? (
    <div>
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <p className="text-sm font-semibold">Coolify logs</p>
          {selectedReleaseLogs?.status ? (
            <p className="mt-1 text-xs text-muted-foreground">Status {selectedReleaseLogs.status}</p>
          ) : null}
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => void loadSelectedReleaseLogs()}
          disabled={selectedReleaseLogsLoading}
        >
          {selectedReleaseLogsLoading ? "Refreshing..." : "Refresh"}
        </Button>
      </div>
      {selectedReleaseLogsError ? (
        <p className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {selectedReleaseLogsError}
        </p>
      ) : selectedReleaseLogsLoading && !selectedReleaseLogs ? (
        <p className="rounded-lg border px-3 py-2 text-xs text-muted-foreground">Loading logs...</p>
      ) : selectedReleaseLogs?.logs.trim() ? (
        <pre
          data-testid="release-logs-output"
          className="max-h-[520px] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-slate-950 p-3 font-mono text-[11px] leading-5 text-slate-100"
        >
          {selectedReleaseLogs.truncated ? "[showing latest log output]\n" : ""}
          {formatProviderLogs(selectedReleaseLogs.logs)}
        </pre>
      ) : (
        <p className="rounded-lg border px-3 py-2 text-xs text-muted-foreground">No logs yet.</p>
      )}
    </div>
  ) : (
    <p className="rounded-lg border px-3 py-2 text-xs text-muted-foreground">
      No provider logs are available for this release.
    </p>
  );

  if (busy && !selectedApp) {
    return (
      <Card>
        <CardContent className="flex min-h-[280px] items-center justify-center text-sm text-muted-foreground">
          Loading deployment...
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3 border-b pb-4">
        <Button asChild variant="ghost" size="sm" className="-ml-2">
          <a href={deploymentsHref}>
            <ArrowLeft className="mr-2 h-4 w-4" />
            Back
          </a>
        </Button>
        <div className="flex min-w-[240px] items-center gap-2">
          <label htmlFor="deployment-selector" className="sr-only">Deployment selector</label>
          <select
            id="deployment-selector"
            value={selectedReleaseId ? `release:${selectedReleaseId}` : selectedApp ? `app:${selectedApp.app_id}` : ""}
            onChange={(event) => {
              const value = event.target.value;
              if (!value) return;
              if (value.startsWith("release:")) {
                const nextReleaseId = value.slice("release:".length);
                setSelectedReleaseId(nextReleaseId);
                if (selectedApp) {
                  router.push(`${deploymentsHref}/${encodeURIComponent(selectedApp.app_id)}?release=${encodeURIComponent(nextReleaseId)}`);
                }
                return;
              }
              const nextAppId = value.startsWith("app:") ? value.slice("app:".length) : value;
              if (!nextAppId) return;
              setSelectedReleaseId(null);
              router.push(`${deploymentsHref}/${encodeURIComponent(nextAppId)}`);
            }}
            className="h-9 w-full rounded-md border bg-background px-3 text-sm font-semibold outline-none transition-colors focus:border-primary"
            disabled={apps.length === 0}
          >
            {selectedApp ? null : <option value="">Select deployment</option>}
            {apps.map((app, index) => (
              <option key={app.app_id} value={`app:${app.app_id}`}>
                {app.app_id === selectedApp?.app_id && latestProductionRelease ? releaseSelectorLabel(latestProductionRelease) : deploymentDisplayName(app, null, index)}
              </option>
            ))}
            {activePreviewReleases.map((release) => (
              <option key={release.release_id} value={`release:${release.release_id}`}>
                {release.release_id === selectedReleaseId ? `Preview: ${selectedDeploymentTitle}` : releaseSelectorLabel(release)}
              </option>
            ))}
          </select>
        </div>
      </div>
      {selectedApp ? (
        <Card id={selectedApp.app_id} className="border-0 bg-transparent shadow-none dark:ring-0">
          <CardHeader className="space-y-4 px-0 pb-0 pt-0">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-2" data-testid="selected-release-heading">
                  <Badge variant="outline">{selectedReleaseLabel}</Badge>
                  <CardTitle className="text-base">{selectedDeploymentTitle}</CardTitle>
                  {selectedStatus ? <Badge variant={appStatusVariant(selectedStatus)} data-testid="selected-release-status">{selectedStatus}</Badge> : null}
                  {latestAnalysisNeedsMerge ? <Badge variant="warning">PR required</Badge> : null}
                </div>
                {latestReleaseIssueUrl ? (
                  <a href={latestReleaseIssueUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-sm text-primary underline-offset-4 hover:underline">
                    Open Jira
                    <ExternalLink className="h-3.5 w-3.5" />
                  </a>
                ) : null}
              </div>
            </div>

            <AppAdminSectionTabs selectedSection={selectedSection} onSectionChange={setSelectedSection} />
          </CardHeader>

          <CardContent className="space-y-4 px-0 pt-6">
            {selectedApp.status === "needs_pr_merge" ? (
              <div className="space-y-3 rounded-xl border border-warning/30 bg-warning/10 px-4 py-3 text-sm text-warning">
                <p>Generated deployment files must be merged before this deployment can run.</p>
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

            {selectedSection === "overview" ? (
              <div className="space-y-6">
                <section className="space-y-6">
                  <div className="flex flex-wrap items-center justify-between gap-3 border-b pb-4">
                    <div>
                      <h2 className="text-sm font-semibold">Deployment details</h2>
                      {latestReleaseSource ? (
                        <p className="mt-1 max-w-3xl truncate text-sm text-muted-foreground">{latestReleaseSource}</p>
                      ) : null}
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <Button variant="outline" size="sm" onClick={() => {
                        setSelectedSection("releases");
                        setSelectedReleaseSection("history");
                      }}>
                        Releases
                      </Button>
                      <Button size="sm" onClick={() => void triggerReleaseAction()} disabled={manualDeployBusy || loadingSurface || !selectedApp}>
                        {manualDeployBusy ? <RefreshCw className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <ArrowRight className="mr-1.5 h-3.5 w-3.5" />}
                        {manualDeployBusy ? (latestReleaseIsPreview ? "Generating..." : "Deploying...") : latestReleaseIsPreview ? "Generate preview" : "Deploy"}
                      </Button>
                      {firstVisitUrl ? (
                        <Button asChild size="sm" className="bg-foreground text-background hover:bg-foreground/90">
                          <a href={firstVisitUrl} target="_blank" rel="noreferrer">
                            Visit
                            <ArrowRight className="ml-1.5 h-3.5 w-3.5" />
                          </a>
                        </Button>
                      ) : null}
                    </div>
                  </div>

                  <div className="grid items-start gap-6 lg:grid-cols-[420px_minmax(0,1fr)]">
                    <Card className="min-w-0 overflow-hidden">
                      {firstVisitUrl ? (
                        <div className="flex min-h-[280px] flex-col bg-background">
                          <div className="flex items-center justify-between gap-3 border-b px-3 py-2">
                            <div className="min-w-0">
                              <p className="truncate text-xs font-medium text-muted-foreground">Preview</p>
                              <p className="truncate text-sm font-semibold">{previewServiceUrl?.service_name || selectedDeploymentTitle}</p>
                            </div>
                            <a
                              href={firstVisitUrl}
                              target="_blank"
                              rel="noreferrer"
                              className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border bg-background text-muted-foreground transition-colors hover:text-foreground"
                              aria-label="Open deployment preview"
                            >
                              <ExternalLink className="h-3.5 w-3.5" />
                            </a>
                          </div>
                          <div className="h-[252px] overflow-hidden bg-white">
                            <iframe
                              key={firstVisitUrl}
                              src={firstVisitUrl}
                              title={`${selectedDeploymentTitle} preview`}
                              className="border-0 bg-white"
                              style={{
                                width: "1440px",
                                height: "900px",
                                transform: "scale(0.28)",
                                transformOrigin: "top left",
                              }}
                              sandbox="allow-forms allow-modals allow-popups allow-same-origin allow-scripts"
                              loading="lazy"
                            />
                          </div>
                        </div>
                      ) : (
                        <div className="flex min-h-[280px] items-center justify-center p-6">
                          <div className="max-w-[260px] text-center">
                            <p className="text-sm font-medium">No deployment URL yet</p>
                            <p className="mt-1 text-sm text-muted-foreground">A matching GitHub push will create the first deployment URL.</p>
                          </div>
                        </div>
                      )}
                    </Card>

                    <Card className="min-w-0">
                      <CardContent className="space-y-6 p-4">
                      <div className="divide-y text-sm">
                        <div className="grid gap-1 py-3 sm:grid-cols-[120px_minmax(0,1fr)]">
                          <span className="text-muted-foreground">Created</span>
                          <span>
                            {relativeTimestamp(latestReleaseCreatedAt)}
                            <span className="ml-2 text-xs text-muted-foreground">{formatTimestamp(latestReleaseCreatedAt)}</span>
                          </span>
                        </div>
                        <div className="grid gap-1 py-3 sm:grid-cols-[120px_minmax(0,1fr)]">
                          <span className="text-muted-foreground">Status</span>
                          <span>
                            <Badge variant={appStatusVariant(latestRelease?.status ?? selectedApp.status)}>
                              {latestRelease?.status ?? selectedApp.status}
                            </Badge>
                          </span>
                        </div>
                        <div className="grid gap-1 py-3 sm:grid-cols-[120px_minmax(0,1fr)]">
                          <span className="text-muted-foreground">Duration</span>
                          <span>{latestReleaseDuration}</span>
                        </div>
                        <div className="grid gap-1 py-3 sm:grid-cols-[120px_minmax(0,1fr)]">
                          <span className="text-muted-foreground">Environment</span>
                          <span className="truncate">{latestReleaseEnvironment}</span>
                        </div>
                        <div className="grid gap-1 py-3 sm:grid-cols-[120px_minmax(0,1fr)]">
                          <span className="text-muted-foreground">Runtime</span>
                          <span>{appRuntimeLabel(selectedApp.detected_runtime)}</span>
                        </div>
                        <div className="grid gap-1 py-3 sm:grid-cols-[120px_minmax(0,1fr)]">
                          <span className="text-muted-foreground">Build</span>
                          <span>{compactBuildStrategyLabel(selectedApp.build_strategy)}</span>
                        </div>
                      </div>

                      <div className="mt-6 space-y-3">
                        <p className="text-sm font-semibold">URLs</p>
                        {latestServiceUrls.length > 0 ? (
                          <div className="divide-y border-y">
                            {latestServiceUrls.map((serviceUrl) => (
                              <div key={`${serviceUrl.service_key}-${serviceUrl.url_kind}-${serviceUrl.domain_key ?? serviceUrl.url}`} className="grid gap-1 py-3 text-sm">
                                <span className="text-xs text-muted-foreground">{serviceUrl.service_name || serviceUrl.service_kind}</span>
                                <a href={serviceUrl.url} target="_blank" rel="noreferrer" className="break-all text-primary underline-offset-4 hover:underline">
                                  {serviceUrl.url}
                                </a>
                              </div>
                            ))}
                          </div>
                        ) : (
                          <p className="text-sm text-muted-foreground">No domains on the latest deployment.</p>
                        )}
                      </div>

                      {latestRelease?.last_error ? (
                        <p className="border-y border-destructive/30 py-3 text-xs text-destructive">
                          {latestRelease.last_error}
                        </p>
                      ) : null}

                      {latestRelease?.provider === "internal_coolify" ? (
                        <div className="border-t pt-4">
                          {releaseLogsContent}
                        </div>
                      ) : null}
                      </CardContent>
                    </Card>
                  </div>

                  <div className="grid border-y md:grid-cols-3 md:divide-x">
                    <button type="button" className="flex w-full items-center justify-between px-1 py-3 text-left text-sm hover:bg-muted/30 md:px-4" onClick={() => setSelectedSection("settings")}>
                      <span className="font-medium">Deployment settings</span>
                      <ArrowRight className="h-4 w-4 text-muted-foreground" />
                    </button>
                    <button
                      type="button"
                      className="flex w-full items-center justify-between border-t px-1 py-3 text-left text-sm hover:bg-muted/30 md:border-t-0 md:px-4"
                      onClick={() => {
                        setSelectedSection("releases");
                        setSelectedReleaseSection("history");
                      }}
                    >
                      <span className="font-medium">Deployment history</span>
                      <span className="text-muted-foreground">{deploymentReleases.length} release{deploymentReleases.length === 1 ? "" : "s"}</span>
                    </button>
                    <button type="button" className="flex w-full items-center justify-between border-t px-1 py-3 text-left text-sm hover:bg-muted/30 md:border-t-0 md:px-4" onClick={() => setSelectedSection("domains")}>
                      <span className="font-medium">Domains</span>
                      <span className="text-muted-foreground">{latestServiceUrls.length} URL{latestServiceUrls.length === 1 ? "" : "s"}</span>
                    </button>
                  </div>
                </section>

                <section className="space-y-3">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <h3 className="text-base font-semibold">Recent deployments</h3>
                    <Button variant="ghost" size="sm" onClick={() => {
                      setSelectedSection("releases");
                      setSelectedReleaseSection("history");
                    }}>
                      View all
                    </Button>
                  </div>
                  <div className="overflow-hidden rounded-xl border">
                    {deploymentReleases.length === 0 ? (
                      <div className="px-5 py-8 text-sm text-muted-foreground">No deployments yet.</div>
                    ) : (
                      deploymentReleases.slice(0, 5).map((release) => (
                        <div key={release.release_id} className="grid gap-3 border-b px-5 py-4 last:border-b-0 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                              <p className="truncate text-sm font-medium">{release.git_ref} @ {release.commit_sha.slice(0, 8)}</p>
                              <Badge variant={appStatusVariant(release.status)}>{release.status}</Badge>
                            </div>
                            <p className="mt-1 truncate font-mono text-xs text-muted-foreground">{release.source_issue_key ? `${release.source_issue_key}${release.source_issue_summary ? `: ${release.source_issue_summary}` : ""}` : release.release_id}</p>
                          </div>
                          <div className="text-sm text-muted-foreground">{relativeTimestamp(release.created_at)}</div>
                        </div>
                      ))
                    )}
                  </div>
                </section>
              </div>
            ) : null}

            {selectedSection === "settings" || selectedSection === "environment" ? (
              <div className={selectedSection === "settings" ? "grid gap-8 lg:grid-cols-[minmax(0,1fr)_240px]" : "space-y-6"}>
                <div className="min-w-0 space-y-6">
                  {selectedSection === "settings" && selectedDeploymentSection === "runtime" ? (
                    <section className="space-y-5">
                      <div>
                        <h3 className="text-base font-semibold">Runtime</h3>
                      </div>
                      <div className="grid gap-4 md:grid-cols-2">
                        <div className="space-y-1.5">
                          <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Environment</label>
                          <Input
                            value={deploymentForm.environment_name}
                            onChange={(event) => setDeploymentForm((current) => ({ ...current, environment_name: event.target.value }))}
                            placeholder="production"
                            disabled={savingConfig || deploying}
                          />
                        </div>
                        <div className="space-y-1.5">
                          <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Source</label>
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
                          <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Build</label>
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
                          <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Public port</label>
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
                      </div>
                    </section>
                  ) : null}

                  {selectedSection === "environment" || selectedDeploymentSection === "environment" ? (
                    <section className="space-y-5">
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <h3 className="text-base font-semibold">Environment</h3>
                        </div>
                        <Button className="shrink-0" size="sm" onClick={() => void saveDeploymentConfig()} disabled={savingConfig || deploying || loadingSurface}>
                          <Save className="mr-1.5 h-3.5 w-3.5" />
                          {savingConfig ? "Saving..." : "Save environment"}
                        </Button>
                      </div>

                      <div className="space-y-8">
                        <div className="space-y-3">
                          <div className="flex flex-wrap items-center justify-between gap-3">
                            <div>
                              <h4 className="text-sm font-medium">Variables</h4>
                            </div>
                            <Button type="button" variant="outline" size="sm" onClick={appendEnvironmentVariable} disabled={savingConfig || deploying}>
                              <Plus className="mr-1.5 h-3.5 w-3.5" />
                              Add variable
                            </Button>
                          </div>
                          {deploymentForm.environment.length === 0 ? (
                            <div className="rounded-xl border border-dashed px-4 py-8 text-sm text-muted-foreground">
                              No deployment-specific variables configured.
                            </div>
                          ) : (
                            <div className="divide-y rounded-xl border">
                              {deploymentForm.environment.map((variable, index) => (
                                <div key={`environment-${index}`} className="grid gap-3 p-4 md:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)_auto] md:items-end">
                                  <div className="space-y-1.5">
                                    <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Name</label>
                                    <Input
                                      value={variable.key}
                                      onChange={(event) => setDeploymentForm((current) => ({
                                        ...current,
                                        environment: current.environment.map((item, currentIndex) => (
                                          currentIndex === index ? { ...item, key: event.target.value } : item
                                        )),
                                      }))}
                                      onPaste={(event) => handleEnvironmentVariablePaste(event, index)}
                                      placeholder="NODE_ENV"
                                      disabled={savingConfig || deploying}
                                    />
                                  </div>
                                  <div className="space-y-1.5">
                                    <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Value</label>
                                    <Input
                                      value={variable.value}
                                      onChange={(event) => setDeploymentForm((current) => ({
                                        ...current,
                                        environment: current.environment.map((item, currentIndex) => (
                                          currentIndex === index ? { ...item, value: event.target.value } : item
                                        )),
                                      }))}
                                      onPaste={(event) => handleEnvironmentVariablePaste(event, index)}
                                      placeholder="production"
                                      disabled={savingConfig || deploying}
                                    />
                                  </div>
                                  <Button
                                    type="button"
                                    variant="ghost"
                                    size="sm"
                                    className="h-9 w-9 p-0"
                                    onClick={() => setDeploymentForm((current) => ({
                                      ...current,
                                      environment: current.environment.filter((_, currentIndex) => currentIndex !== index),
                                    }))}
                                    disabled={savingConfig || deploying}
                                    aria-label={`Remove environment variable ${variable.key || index + 1}`}
                                  >
                                    <Trash2 className="h-4 w-4" />
                                  </Button>
                                </div>
                              ))}
                            </div>
                          )}
                        </div>

                        <div className="space-y-3">
                          <div className="flex flex-wrap items-center justify-between gap-3">
                            <div>
                              <h4 className="text-sm font-medium">Secrets</h4>
                            </div>
                            <Button type="button" variant="outline" size="sm" onClick={appendSecretReference} disabled={savingConfig || deploying}>
                              <Plus className="mr-1.5 h-3.5 w-3.5" />
                              Add secret
                            </Button>
                          </div>
                          {deploymentForm.secret_refs.length === 0 ? (
                            <div className="rounded-xl border border-dashed px-4 py-8 text-sm text-muted-foreground">
                              No deployment-specific secret references configured.
                            </div>
                          ) : (
                            <div className="divide-y rounded-xl border">
                              {deploymentForm.secret_refs.map((secret, index) => (
                                <div key={`secret-${index}`} className="grid gap-3 p-4 md:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)_auto] md:items-end">
                                  <div className="space-y-1.5">
                                    <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Environment name</label>
                                    <Input
                                      value={secret.key}
                                      onChange={(event) => setDeploymentForm((current) => ({
                                        ...current,
                                        secret_refs: current.secret_refs.map((item, currentIndex) => (
                                          currentIndex === index ? { ...item, key: event.target.value } : item
                                        )),
                                      }))}
                                      placeholder="DATABASE_URL"
                                      disabled={savingConfig || deploying}
                                    />
                                  </div>
                                  <div className="space-y-1.5">
                                    <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Secret reference</label>
                                    <Input
                                      value={secret.secret_ref}
                                      onChange={(event) => setDeploymentForm((current) => ({
                                        ...current,
                                        secret_refs: current.secret_refs.map((item, currentIndex) => (
                                          currentIndex === index ? { ...item, secret_ref: event.target.value } : item
                                        )),
                                      }))}
                                      placeholder="tenant/bsktpay-2/DATABASE_URL"
                                      disabled={savingConfig || deploying}
                                    />
                                  </div>
                                  <Button
                                    type="button"
                                    variant="ghost"
                                    size="sm"
                                    className="h-9 w-9 p-0"
                                    onClick={() => setDeploymentForm((current) => ({
                                      ...current,
                                      secret_refs: current.secret_refs.filter((_, currentIndex) => currentIndex !== index),
                                    }))}
                                    disabled={savingConfig || deploying}
                                    aria-label={`Remove secret ${secret.key || index + 1}`}
                                  >
                                    <Trash2 className="h-4 w-4" />
                                  </Button>
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      </div>
                    </section>
                  ) : null}

                  {selectedSection === "settings" && selectedDeploymentSection === "health" ? (
                    <section className="space-y-5">
                      <div>
                        <h3 className="text-base font-semibold">Health check</h3>
                      </div>
                      <div className="max-w-xl space-y-1.5">
                        <label htmlFor="app-healthcheck" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Path or URL</label>
                        <Input
                          id="app-healthcheck"
                          value={deploymentForm.healthcheckText}
                          onChange={(event) => setDeploymentForm((current) => ({ ...current, healthcheckText: event.target.value }))}
                          placeholder="/health"
                          disabled={savingConfig || deploying}
                        />
                      </div>
                    </section>
                  ) : null}
                </div>

                {selectedSection === "settings" ? (
                  <aside className="space-y-4 lg:sticky lg:top-6 lg:self-start">
                    <SideNavigation
                      label="Deployment settings"
                      items={DEPLOYMENT_SECTIONS}
                      selected={selectedDeploymentSection}
                      onSelect={setSelectedDeploymentSection}
                    />
                    <Button className="w-full" size="sm" onClick={() => void saveDeploymentConfig()} disabled={savingConfig || deploying || loadingSurface}>
                      <Save className="mr-1.5 h-3.5 w-3.5" />
                      {savingConfig ? "Saving..." : "Save"}
                    </Button>
                  </aside>
                ) : null}
              </div>
            ) : null}

            {selectedSection === "services" ? (
              <section className="space-y-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <h3 className="text-base font-semibold">Services</h3>
                  <Button size="sm" onClick={() => void saveDeploymentConfig()} disabled={savingConfig || deploying || loadingSurface}>
                    <Save className="mr-1.5 h-3.5 w-3.5" />
                    {savingConfig ? "Saving..." : "Save services"}
                  </Button>
                </div>
                {serviceRows.length === 0 ? (
                  <p className="rounded-xl border border-dashed px-4 py-6 text-sm text-muted-foreground">No services configured yet.</p>
                ) : (
                  <div className="divide-y rounded-xl border">
                    {serviceRows.map((service) => (
                      <div key={service.key} className="grid gap-4 p-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,0.8fr)_minmax(0,0.7fr)]">
                        <div className="min-w-0 space-y-2">
                          <div className="flex flex-wrap items-center gap-2">
                            <p className="truncate text-sm font-semibold">{service.name}</p>
                            <Badge variant="outline">{service.serviceType}</Badge>
                            <Badge variant="outline">{service.public ? "public" : "internal"}</Badge>
                          </div>
                          <div className="space-y-1 text-xs text-muted-foreground">
                            <p className="truncate">{service.sourcePath ?? "Source not recorded"}</p>
                            <p className="truncate">{service.composeService}</p>
                          </div>
                        </div>
                        <div className="space-y-1.5">
                          <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Build</label>
                          <select
                            className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                            value={service.buildStrategy}
                            onChange={(event) => updateServiceBuildStrategy(service.resourceIndex, event.target.value)}
                            disabled={savingConfig || deploying}
                          >
                            <option value="">Auto</option>
                            {SERVICE_BUILD_STRATEGY_OPTIONS.map((option) => (
                              <option key={option.value} value={option.value}>{option.label}</option>
                            ))}
                          </select>
                        </div>
                        <div className="space-y-1.5">
                          <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Port</span>
                          <p className="h-10 rounded-md border bg-muted/20 px-3 py-2 text-sm">{service.containerPort ?? "—"}</p>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </section>
            ) : null}

            {selectedSection === "resources" ? (
              <Card>
                <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
                  <div>
                    <CardTitle className="text-sm">Resources</CardTitle>
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
                  {infrastructureResourceEntries.length === 0 ? (
                    <p className="rounded-xl border border-dashed px-4 py-6 text-sm text-muted-foreground">No resources configured yet.</p>
                  ) : (
                    infrastructureResourceEntries.map(({ resource, index }) => (
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
                          <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Configuration</span>
                          <ConfigSummary configText={resource.configText} />
                        </div>
                      </div>
                    ))
                  )}
                  {operationFeedback.resources.statusLine ? <p className="text-xs text-muted-foreground">{operationFeedback.resources.statusLine}</p> : null}
                </CardContent>
              </Card>
            ) : null}

            {selectedSection === "volumes" ? (
              <Card>
                <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
                  <div>
                    <CardTitle className="text-sm">Volumes</CardTitle>
                  </div>
                  <div className="flex items-center gap-2">
                    <Button variant="outline" size="sm" onClick={() => appendVolume("persistent")} disabled={savingConfig || deploying}>
                      <Plus className="mr-1.5 h-3.5 w-3.5" />
                      Add volume
                    </Button>
                    <Button size="sm" onClick={() => void applyVolumes()} disabled={savingConfig || deploying}>
                      Apply volumes
                    </Button>
                  </div>
                </CardHeader>
                <CardContent className="space-y-3">
                  {deploymentForm.volumes.filter((volume) => !isBlankVolumeDraft(volume)).length === 0 ? (
                    <p className="rounded-xl border border-dashed px-4 py-6 text-sm text-muted-foreground">No volumes configured yet.</p>
                  ) : (
                    deploymentForm.volumes.map((volume, index) => (
                      <div key={`${volume.key || "volume"}-${index}`} className="grid gap-3 rounded-xl border p-4 xl:grid-cols-[1fr_1fr_1.2fr_1.4fr]">
                        <div className="space-y-1.5">
                          <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Key</label>
                          <Input value={volume.key} onChange={(event) => setDeploymentForm((current) => ({
                            ...current,
                            volumes: current.volumes.map((item, currentIndex) => (currentIndex === index ? { ...item, key: event.target.value } : item)),
                          }))} />
                        </div>
                        <div className="space-y-1.5">
                          <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Type</label>
                          <select
                            className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                            value={volume.type}
                            onChange={(event) => setDeploymentForm((current) => ({
                              ...current,
                              volumes: current.volumes.map((item, currentIndex) => (
                                currentIndex === index ? { ...item, type: event.target.value === "file" ? "file" : "persistent" } : item
                              )),
                            }))}
                          >
                            {VOLUME_TYPE_OPTIONS.map((option) => (
                              <option key={option.value} value={option.value}>{option.label}</option>
                            ))}
                          </select>
                        </div>
                        <div className="space-y-1.5">
                          <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Name</label>
                          <Input value={volume.name} onChange={(event) => setDeploymentForm((current) => ({
                            ...current,
                            volumes: current.volumes.map((item, currentIndex) => (currentIndex === index ? { ...item, name: event.target.value } : item)),
                          }))} />
                        </div>
                        <div className="space-y-1.5">
                          <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Configuration</span>
                          <ConfigSummary configText={volume.configText} />
                        </div>
                      </div>
                    ))
                  )}
                  {operationFeedback.volumes.statusLine ? <p className="text-xs text-muted-foreground">{operationFeedback.volumes.statusLine}</p> : null}
                </CardContent>
              </Card>
            ) : null}

            {selectedSection === "domains" ? (
              <div className="space-y-4">
                <Card>
                  <CardHeader>
                    <CardTitle className="text-sm">Generated release URLs</CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    {latestServiceUrlGroups.length === 0 ? (
                      <p className="rounded-xl border border-dashed px-4 py-6 text-sm text-muted-foreground">No generated release URLs yet.</p>
                    ) : (
                      latestServiceUrlGroups.map((service) => (
                        <div key={service.serviceKey} className="rounded-xl border p-4">
                          <div className="flex flex-wrap items-center gap-2">
                            <p className="text-sm font-medium">{service.serviceName}</p>
                            <Badge variant="outline">{service.serviceKind}</Badge>
                          </div>
                          <div className="mt-3 space-y-1">
                            {service.urls.map((serviceUrl) => (
                              <div key={`${serviceUrl.url_kind}-${serviceUrl.domain_key ?? serviceUrl.url}`} className="space-y-1">
                                <a
                                  href={serviceUrl.url}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="block truncate text-sm text-primary underline-offset-4 hover:underline"
                                >
                                  {serviceUrl.url}
                                </a>
                                {serviceUrl.internal_url ? (
                                  <p className="truncate font-mono text-[11px] text-muted-foreground">
                                    {serviceUrl.internal_url} {"->"} {serviceUrl.host ?? serviceUrl.url}
                                  </p>
                                ) : null}
                              </div>
                            ))}
                          </div>
                        </div>
                      ))
                    )}
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
                    <div>
                      <CardTitle className="text-sm">Custom domains</CardTitle>
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
                      <p className="rounded-xl border border-dashed px-4 py-6 text-sm text-muted-foreground">No custom domains configured.</p>
                    ) : (
                      deploymentForm.domains.map((domain, index) => (
                        <div key={`${domain.key || "domain"}-${index}`} className="grid gap-3 rounded-xl border p-4 xl:grid-cols-[1fr_1fr_1.6fr_1fr]">
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
                            <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Service</label>
                            <Input
                              value={domain.service_key}
                              onChange={(event) => setDeploymentForm((current) => ({
                                ...current,
                                domains: current.domains.map((item, currentIndex) => (currentIndex === index ? { ...item, service_key: event.target.value } : item)),
                              }))}
                              placeholder="web"
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
                            <label className="inline-flex items-center gap-2 text-sm">
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
                      ))
                    )}
                    {operationFeedback.domains.statusLine ? <p className="text-xs text-muted-foreground">{operationFeedback.domains.statusLine}</p> : null}
                  </CardContent>
                </Card>
              </div>
            ) : null}

            {selectedSection === "backups" ? (
              <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_240px]">
                <div className="min-w-0 space-y-6">
                  {selectedBackupSection === "policies" ? (
                    <section className="space-y-4">
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <h3 className="text-base font-semibold">Backup policies</h3>
                        </div>
                        <div className="flex items-center gap-2">
                          <Button variant="outline" size="sm" onClick={appendBackupPolicy} disabled={savingConfig || deploying}>
                            <Plus className="mr-1.5 h-3.5 w-3.5" />
                            Add policy
                          </Button>
                          <Button size="sm" onClick={() => void applyBackups()} disabled={savingConfig || deploying}>
                            Apply
                          </Button>
                        </div>
                      </div>
                      {deploymentForm.backup_policies.length === 0 ? (
                        <p className="rounded-xl border border-dashed px-4 py-8 text-sm text-muted-foreground">No backup policies configured yet.</p>
                      ) : (
                        <div className="divide-y rounded-xl border">
                          {deploymentForm.backup_policies.map((backup, index) => (
                            <div key={`${backup.key || "backup"}-${index}`} className="grid gap-3 p-4 xl:grid-cols-[1fr_1fr_1fr_1fr]">
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
                          ))}
                        </div>
                      )}
                      {operationFeedback.backups.statusLine ? <p className="text-xs text-muted-foreground">{operationFeedback.backups.statusLine}</p> : null}
                    </section>
                  ) : null}

                  {selectedBackupSection === "backup-now" ? (
                    <section className="space-y-4">
                      <div>
                        <h3 className="text-base font-semibold">Backup now</h3>
                      </div>
                      <div className="max-w-lg space-y-1.5">
                        <label htmlFor="backup-now-policy-select" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Policy</label>
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
                      {operationFeedback.backupNow.statusLine ? (
                        <div className={`rounded-xl border px-3 py-2 text-xs ${statusLineVariant(operationFeedback.backupNow.result?.status ?? operationFeedback.backupNow.statusLine)}`}>
                          {operationFeedback.backupNow.statusLine}
                        </div>
                      ) : null}
                      {operationFeedback.backupNow.result ? (
                        <OperationResultSummary result={operationFeedback.backupNow.result} />
                      ) : null}
                    </section>
                  ) : null}

                  {selectedBackupSection === "restore" ? (
                    <section className="space-y-5">
                      <div>
                        <h3 className="text-base font-semibold">Restore</h3>
                      </div>
                      <div className="grid gap-3 md:grid-cols-2">
                        <div className="space-y-1.5">
                          <label htmlFor="restore-backup-policy-select" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Backup policy</label>
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
                          <label htmlFor="restore-resource-select" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Resource</label>
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
                          <label htmlFor="restore-execution-select" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Execution</label>
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
                        <div className="space-y-1.5">
                          <label htmlFor="restore-confirmation-input" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                            Confirm deployment slug
                          </label>
                          <Input
                            id="restore-confirmation-input"
                            value={restoreConfirmationValue}
                            onChange={(event) => setRestoreConfirmationValue(event.target.value)}
                            placeholder={selectedApp?.slug ?? "deployment-slug"}
                          />
                        </div>
                      </div>
                      <div className={`rounded-xl border px-3 py-2 text-xs ${statusLineVariant(restoreBusy ? "running" : latestRestoreRun?.status ?? "queued")}`}>
                        {restoreStatusLine || (latestRestoreRun ? `Latest restore run ${latestRestoreRun.restore_run_id} is ${latestRestoreRun.status}.` : "Choose a backup execution and confirm the deployment slug to start a restore.")}
                      </div>
                      <div className="space-y-3">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div>
                            <h4 className="text-sm font-medium">Available executions</h4>
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
                          <div className="rounded-xl border border-dashed px-4 py-8 text-sm text-muted-foreground">
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
                      <div className="space-y-3">
                        <h4 className="text-sm font-medium">Restore history</h4>
                        {restoreRunList.length === 0 ? (
                          <div className="rounded-xl border border-dashed px-4 py-8 text-sm text-muted-foreground">No restore runs recorded for this deployment yet.</div>
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
                      </div>
                    </section>
                  ) : null}
                </div>

                <aside className="space-y-4 lg:sticky lg:top-6 lg:self-start">
                  <SideNavigation
                    label="Backup settings"
                    items={BACKUP_SECTIONS}
                    selected={selectedBackupSection}
                    onSelect={setSelectedBackupSection}
                  />
                  {selectedBackupSection === "backup-now" ? (
                    <Button className="w-full" size="sm" onClick={() => void backupNow()} disabled={savingConfig || deploying || operationFeedback.backupNow.busy}>
                      {operationFeedback.backupNow.busy ? <RefreshCw className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <ArrowRight className="mr-1.5 h-3.5 w-3.5" />}
                      Run backup
                    </Button>
                  ) : null}
                  {selectedBackupSection === "restore" ? (
                    <Button className="w-full" size="sm" variant="outline" onClick={() => void requestRestore()} disabled={savingConfig || deploying || restoreBusy || !restoreSelectionIsValid}>
                      {restoreBusy ? <RefreshCw className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <ArrowRight className="mr-1.5 h-3.5 w-3.5" />}
                      {restoreBusy ? "Queuing…" : "Run restore"}
                    </Button>
                  ) : null}
                </aside>
              </div>
            ) : null}

            {selectedSection === "releases" ? (
              <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_240px]">
                <div className="min-w-0 space-y-6">
                  {selectedReleaseSection === "history" ? (
                    <section className="space-y-4">
                      <div className="grid gap-3 rounded-xl border p-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] md:items-end">
                        <div className="space-y-1.5">
                          <label htmlFor="manual-deploy-branch" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Branch</label>
                          <select
                            id="manual-deploy-branch"
                            className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                            value={manualDeployGitRef}
                            onChange={(event) => {
                              setManualDeployGitRef(event.target.value);
                              setManualDeployCommitSha("");
                            }}
                            disabled={manualDeployBusy || manualDeployBranchOptions.length === 0}
                          >
                            {manualDeployBranchOptions.map((branch) => (
                              <option key={branch} value={branch}>
                                {branch}
                              </option>
                            ))}
                          </select>
                        </div>
                        <div className="space-y-1.5">
                          <label htmlFor="manual-deploy-commit" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Commit</label>
                          <select
                            id="manual-deploy-commit"
                            className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                            value={manualDeployCommitSha}
                            onChange={(event) => setManualDeployCommitSha(event.target.value)}
                            disabled={manualDeployBusy || manualDeployCommitOptions.length === 0}
                          >
                            {manualDeployCommitOptions.map((commitSha) => (
                              <option key={commitSha} value={commitSha}>
                                {commitSha}
                              </option>
                            ))}
                          </select>
                        </div>
                        <Button onClick={() => void triggerManualDeploy()} disabled={manualDeployBusy || loadingSurface || !selectedApp || manualDeployBranchOptions.length === 0 || manualDeployCommitOptions.length === 0}>
                          {manualDeployBusy ? <RefreshCw className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <ArrowRight className="mr-1.5 h-3.5 w-3.5" />}
                          {manualDeployBusy ? "Deploying..." : "Deploy"}
                        </Button>
                      </div>
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div>
                          <h3 className="text-base font-semibold">Release history</h3>
                        </div>
                        <Badge variant="outline">{deploymentReleases.length} total</Badge>
                      </div>
                    <div className="overflow-hidden rounded-xl border">
                    {deploymentReleases.length === 0 ? (
                      <div className="px-6 py-12 text-center text-sm text-muted-foreground">No releases found for this deployment.</div>
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
                          {deploymentReleases.map((release) => (
                            <TableRow key={release.release_id}>
                              <TableCell>
                                <div className="space-y-1">
                                  <div className="flex flex-wrap items-center gap-2">
                                    <p className="font-medium">{release.git_ref} @ {release.commit_sha.slice(0, 8)}</p>
                                  </div>
                                  <p className="font-mono text-xs text-muted-foreground">{release.release_id}</p>
                                  {release.source_run_id ? (
                                    <p className="font-mono text-xs text-muted-foreground">Run {release.source_run_id}</p>
                                  ) : null}
                                </div>
                              </TableCell>
                              <TableCell>
                                <Badge variant={appStatusVariant(release.status)}>{release.status}</Badge>
                              </TableCell>
                              <TableCell className="max-w-[280px] truncate text-sm text-muted-foreground">{release.commit_sha}</TableCell>
                              <TableCell className="text-sm text-muted-foreground">{formatTimestamp(release.created_at)}</TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    )}
                    </div>
                    </section>
                  ) : null}
                </div>

                <aside className="space-y-4 lg:sticky lg:top-6 lg:self-start">
                  <SideNavigation
                    label="Release settings"
                    items={RELEASE_SECTIONS}
                    selected={selectedReleaseSection}
                    onSelect={setSelectedReleaseSection}
                  />
                </aside>
              </div>
            ) : null}

            {selectedSection === "diagnostics" ? (
              <div className="space-y-4">
                <Card>
                  <CardHeader>
                    <div>
                      <CardTitle className="text-sm">Release logs</CardTitle>
                      {latestReleaseSource ? (
                        <p className="mt-1 text-xs text-muted-foreground">{latestReleaseSource}</p>
                      ) : null}
                    </div>
                  </CardHeader>
                  <CardContent>{releaseLogsContent}</CardContent>
                </Card>

                {latestRelease?.last_error ? (
                  <Card className="border-destructive/30">
                    <CardHeader>
                      <CardTitle className="text-sm text-destructive">Release error</CardTitle>
                    </CardHeader>
                    <CardContent>
                      <p className="whitespace-pre-wrap text-sm text-destructive">{latestRelease.last_error}</p>
                    </CardContent>
                  </Card>
                ) : null}
              </div>
            ) : null}

            {selectedSection === "danger" ? (
              <section className="space-y-5 rounded-2xl border border-destructive/30 bg-destructive/[0.03] p-5">
                <div className="max-w-2xl space-y-2">
                  <h3 className="text-base font-semibold text-destructive">Remove deployment</h3>
                  <p className="text-sm text-muted-foreground">
                    Remove this deployment from Master Builder. Active deployments and deployment host commands must finish first.
                  </p>
                </div>
                <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
                  <div className="space-y-1.5">
                    <label htmlFor="remove-app-confirmation" className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                      Type deployment slug to confirm
                    </label>
                    <Input
                      id="remove-app-confirmation"
                      value={deleteConfirmationValue}
                      onChange={(event) => setDeleteConfirmationValue(event.target.value)}
                      placeholder={selectedApp.slug}
                      disabled={deleteBusy}
                    />
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    className="border-destructive/40 text-destructive hover:bg-destructive/10 hover:text-destructive"
                    onClick={() => void removeSelectedApp()}
                    disabled={deleteBusy || !deleteConfirmationMatches}
                  >
                    <Trash2 className="mr-1.5 h-3.5 w-3.5" />
                    {deleteBusy ? "Removing…" : "Remove deployment"}
                  </Button>
                </div>
              </section>
            ) : null}

            {selectedSection !== "settings" && selectedSection !== "danger" ? (
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
            <p className="mt-4 text-sm font-medium">Select a deployment to review details</p>
            <p className="mt-1 text-sm text-muted-foreground">Configure the project deployment policy first.</p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
