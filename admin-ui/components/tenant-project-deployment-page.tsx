"use client";

import { Fragment, useEffect, useMemo, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import {
  applyProjectDeploymentBackups,
  applyProjectDeploymentDomains,
  applyProjectDeploymentResources,
  requestProjectDeploymentBackupNow,
  requestProjectDeploymentRestore,
  createProjectDeploymentRelease,
  getProjectDeploymentConfig,
  listProjectDeploymentReleases,
  updateProjectDeploymentConfig,
  type Credentials,
  type ProjectDeploymentBackupPolicyRecord,
  type ProjectDeploymentConfigRecord,
  type ProjectDeploymentDomainRecord,
  type ProjectDeploymentOperationResultRecord,
  type ProjectDeploymentReleaseCreatePayload,
  type ProjectDeploymentReleaseRecord,
  type ProjectDeploymentResourceRecord,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { ChevronDown, ChevronUp, CircleAlert, Plus, RefreshCw, Save, Trash2 } from "lucide-react";

type ProjectDeploymentContentProps = {
  tenantId: string;
  projectId: string;
  credentials: Credentials | null;
};

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
  domains: DeploymentDomainDraft[];
  resources: DeploymentResourceDraft[];
  backup_policies: DeploymentBackupDraft[];
};

type ReleaseDraft = {
  git_ref: string;
  commit_sha: string;
  reason: string;
};

type OperationKey = "resources" | "domains" | "backups" | "backupNow" | "restore";

type OperationFeedback = {
  busy: boolean;
  statusLine: string;
  result: ProjectDeploymentOperationResultRecord | null;
};

const RESOURCE_KIND_OPTIONS = [
  { value: "postgres", label: "PostgreSQL database" },
  { value: "mysql", label: "MySQL database" },
  { value: "redis", label: "Redis cache" },
  { value: "s3", label: "Object storage (S3)" },
  { value: "volume", label: "Persistent volume" },
  { value: "custom", label: "Custom resource" },
];

function emptyDeploymentForm(): DeploymentFormState {
  return {
    enabled: true,
    environment_name: "",
    source_strategy: "dockerfile",
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

function parseJsonText(value: string): Record<string, unknown> {
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

function uniqueDraftKey(prefix: string, existingKeys: string[]): string {
  const normalizedPrefix = prefix.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "item";
  let candidate = normalizedPrefix;
  let suffix = 2;
  const taken = new Set(existingKeys.map((item) => item.trim().toLowerCase()).filter(Boolean));
  while (taken.has(candidate)) {
    candidate = `${normalizedPrefix}-${suffix}`;
    suffix += 1;
  }
  return candidate;
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

function toDeploymentForm(record: ProjectDeploymentConfigRecord | null): DeploymentFormState {
  if (!record) {
    return emptyDeploymentForm();
  }
  return {
    enabled: record.enabled !== false,
    environment_name: record.environment_name ?? "",
    source_strategy: record.source_strategy ?? "",
    domains: (record.domains ?? []).map((domain) => toDomainDraft(domain)),
    resources: (record.resources ?? []).map((resource) => toResourceDraft(resource)),
    backup_policies: (record.backup_policies ?? []).map((backup) => toBackupDraft(backup)),
  };
}

function buildDeploymentPayload(form: DeploymentFormState): ProjectDeploymentConfigRecord {
  const domains = form.domains
    .filter((domain) => !isBlankDomainDraft(domain))
    .map((domain) => ({
      key: domain.key.trim().toLowerCase(),
      host: domain.host.trim().toLowerCase(),
      path: trimToNull(domain.path),
      is_primary: domain.is_primary,
      tls_enabled: domain.tls_enabled,
    }));

  const resources = form.resources
    .filter((resource) => !isBlankResourceDraft(resource))
    .map((resource) => ({
      key: resource.key.trim().toLowerCase(),
      kind: resource.kind.trim().toLowerCase(),
      name: trimToNull(resource.name),
      config: parseJsonText(resource.configText),
    }));

  const backupPolicies = form.backup_policies
    .filter((backup) => !isBlankBackupDraft(backup))
    .map((backup) => ({
      key: backup.key.trim().toLowerCase(),
      resource_key: backup.resource_key.trim().toLowerCase(),
      enabled: backup.enabled,
      schedule: trimToNull(backup.schedule),
      retention_days: backup.retention_days.trim() ? Number(backup.retention_days.trim()) : null,
      config: parseJsonText(backup.configText),
    }));

  return {
    enabled: form.enabled,
    environment_name: trimToNull(form.environment_name),
    source_strategy: form.source_strategy || null,
    domains,
    resources,
    backup_policies: backupPolicies,
  };
}

function deploymentStatusVariant(status: string): "default" | "secondary" | "outline" | "success" | "warning" | "destructive" | "info" {
  const normalized = status.trim().toLowerCase();
  if (normalized === "live") {
    return "success";
  }
  if (normalized === "failed") {
    return "destructive";
  }
  if (normalized === "deploying" || normalized === "provisioning") {
    return "warning";
  }
  if (normalized === "queued") {
    return "info";
  }
  return "outline";
}

function formatTimestamp(value: string | null): string {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function normalizeBackupRetention(value: string): string {
  const parsed = Number(value.trim());
  return Number.isFinite(parsed) && parsed > 0 ? String(Math.trunc(parsed)) : "";
}

function validateDeploymentForm(form: DeploymentFormState): string | null {
  const domains = form.domains.filter((domain) => !isBlankDomainDraft(domain));
  const resources = form.resources.filter((resource) => !isBlankResourceDraft(resource));
  const backups = form.backup_policies.filter((backup) => !isBlankBackupDraft(backup));

  const domainKeys = new Set<string>();
  const domainHosts = new Set<string>();
  for (const domain of domains) {
    const key = domain.key.trim().toLowerCase();
    const host = domain.host.trim().toLowerCase();
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
    const key = resource.key.trim().toLowerCase();
    const kind = resource.kind.trim().toLowerCase();
    if (!key || !kind) {
      return "Each resource needs a key and kind.";
    }
    if (resourceKeys.has(key)) {
      return `Duplicate resource key: ${key}`;
    }
    resourceKeys.add(key);
    try {
      parseJsonText(resource.configText);
    } catch (error) {
      return `Invalid JSON for resource ${key}: ${(error as Error).message}`;
    }
  }

  const backupKeys = new Set<string>();
  for (const backup of backups) {
    const key = backup.key.trim().toLowerCase();
    const resourceKey = backup.resource_key.trim().toLowerCase();
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
      parseJsonText(backup.configText);
    } catch (error) {
      return `Invalid JSON for backup policy ${key}: ${(error as Error).message}`;
    }
  }

  return null;
}

function resourceKindLabel(kind: string): string {
  return RESOURCE_KIND_OPTIONS.find((option) => option.value === kind)?.label ?? kind;
}

function deploymentReleaseSummary(release: ProjectDeploymentReleaseRecord): string {
  const gitRef = release.git_ref?.trim();
  const commitSha = release.commit_sha?.trim();
  if (gitRef && commitSha) {
    return `${gitRef} · ${commitSha.slice(0, 8)}`;
  }
  if (gitRef) {
    return gitRef;
  }
  if (commitSha) {
    return commitSha.slice(0, 8);
  }
  return "Latest config";
}

function emptyOperationFeedback(): OperationFeedback {
  return {
    busy: false,
    statusLine: "",
    result: null,
  };
}

export function ProjectDeploymentContent({
  tenantId,
  projectId,
  credentials,
}: ProjectDeploymentContentProps) {
  const { principal } = useAuth();
  const canOverrideStatus = principal?.principal_type === "platform_super_admin";

  const [deploymentForm, setDeploymentForm] = useState<DeploymentFormState>(() => emptyDeploymentForm());
  const [releaseDraft, setReleaseDraft] = useState<ReleaseDraft>(() => emptyReleaseDraft());
  const [releases, setReleases] = useState<ProjectDeploymentReleaseRecord[]>([]);
  const [statusLine, setStatusLine] = useState("");
  const [loading, setLoading] = useState(false);
  const [savingConfig, setSavingConfig] = useState(false);
  const [deploying, setDeploying] = useState(false);
  const [operationFeedback, setOperationFeedback] = useState<Record<OperationKey, OperationFeedback>>({
    resources: emptyOperationFeedback(),
    domains: emptyOperationFeedback(),
    backups: emptyOperationFeedback(),
    backupNow: emptyOperationFeedback(),
    restore: emptyOperationFeedback(),
  });
  const [backupNowResourceKey, setBackupNowResourceKey] = useState("");
  const [backupNowPolicyKey, setBackupNowPolicyKey] = useState("");
  const [restoreResourceKey, setRestoreResourceKey] = useState("");
  const [restoreBackupKey, setRestoreBackupKey] = useState("");
  const [restoreMode, setRestoreMode] = useState<"replace" | "clone">("replace");
  const [restoreNote, setRestoreNote] = useState("");
  const [expandedReleaseId, setExpandedReleaseId] = useState<string | null>(null);

  const activeRelease = useMemo(() => releases[0] ?? null, [releases]);
  const releaseCounts = useMemo(() => {
    return releases.reduce(
      (accumulator, release) => {
        const normalized = release.status.trim().toLowerCase();
        accumulator.total += 1;
        if (normalized === "live") accumulator.live += 1;
        if (normalized === "failed") accumulator.failed += 1;
        if (normalized === "deploying" || normalized === "provisioning") accumulator.active += 1;
        return accumulator;
      },
      { total: 0, live: 0, active: 0, failed: 0 },
    );
  }, [releases]);

  async function loadDeployment() {
    if (!credentials) {
      return;
    }
    setLoading(true);
    try {
      const [config, releaseList] = await Promise.all([
        getProjectDeploymentConfig(credentials, tenantId, projectId),
        listProjectDeploymentReleases(credentials, tenantId, projectId),
      ]);
      setDeploymentForm(toDeploymentForm(config));
      setReleases(releaseList);
      setStatusLine("");
      if (releaseList.length > 0) {
        setExpandedReleaseId((current) => current ?? releaseList[0].release_id);
      }
    } catch (error) {
      setStatusLine(`Unable to load deployment surfaces: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  function updateOperationFeedback(key: OperationKey, updates: Partial<OperationFeedback>) {
    setOperationFeedback((current) => ({
      ...current,
      [key]: {
        ...current[key],
        ...updates,
      },
    }));
  }

  async function runOperation(
    key: OperationKey,
    runner: () => Promise<ProjectDeploymentOperationResultRecord>,
  ) {
    if (!credentials) {
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
    } catch (error) {
      updateOperationFeedback(key, {
        busy: false,
        statusLine: `Operation failed: ${(error as Error).message}`,
        result: null,
      });
    }
  }

  useEffect(() => {
    if (!credentials) {
      return;
    }
    void loadDeployment();
  }, [credentials, tenantId, projectId]);

  useEffect(() => {
    const firstResourceKey = deploymentForm.resources.find((resource) => !isBlankResourceDraft(resource))?.key?.trim() ?? "";
    const firstBackupPolicyKey = deploymentForm.backup_policies.find((policy) => !isBlankBackupDraft(policy))?.key?.trim() ?? "";
    if (!backupNowResourceKey && firstResourceKey) {
      setBackupNowResourceKey(firstResourceKey);
    }
    if (!restoreResourceKey && firstResourceKey) {
      setRestoreResourceKey(firstResourceKey);
    }
    if (!backupNowPolicyKey && firstBackupPolicyKey) {
      setBackupNowPolicyKey(firstBackupPolicyKey);
    }
  }, [deploymentForm.resources, deploymentForm.backup_policies, backupNowResourceKey, backupNowPolicyKey, restoreResourceKey]);

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

  async function saveDeploymentConfig() {
    if (!credentials) {
      return;
    }
    const validationError = validateDeploymentForm(deploymentForm);
    if (validationError) {
      setStatusLine(validationError);
      return;
    }
    setSavingConfig(true);
    try {
      const updated = await updateProjectDeploymentConfig(
        credentials,
        tenantId,
        projectId,
        buildDeploymentPayload(deploymentForm),
      );
      setDeploymentForm(toDeploymentForm(updated));
      setStatusLine("Deployment config saved.");
    } catch (error) {
      setStatusLine(`Save failed: ${(error as Error).message}`);
    } finally {
      setSavingConfig(false);
    }
  }

  async function applyResources() {
    if (!credentials) {
      return;
    }
    const validationError = validateDeploymentForm(deploymentForm);
    if (validationError) {
      updateOperationFeedback("resources", {
        busy: false,
        statusLine: validationError,
        result: null,
      });
      return;
    }
    const payload = buildDeploymentPayload(deploymentForm);
    await runOperation("resources", () =>
      applyProjectDeploymentResources(credentials, tenantId, projectId, {
        resources: payload.resources,
      }),
    );
  }

  async function applyDomains() {
    if (!credentials) {
      return;
    }
    const validationError = validateDeploymentForm(deploymentForm);
    if (validationError) {
      updateOperationFeedback("domains", {
        busy: false,
        statusLine: validationError,
        result: null,
      });
      return;
    }
    const payload = buildDeploymentPayload(deploymentForm);
    await runOperation("domains", () =>
      applyProjectDeploymentDomains(credentials, tenantId, projectId, {
        domains: payload.domains,
      }),
    );
  }

  async function applyBackups() {
    if (!credentials) {
      return;
    }
    const validationError = validateDeploymentForm(deploymentForm);
    if (validationError) {
      updateOperationFeedback("backups", {
        busy: false,
        statusLine: validationError,
        result: null,
      });
      return;
    }
    const payload = buildDeploymentPayload(deploymentForm);
    await runOperation("backups", () =>
      applyProjectDeploymentBackups(credentials, tenantId, projectId, {
        backup_policies: payload.backup_policies,
      }),
    );
  }

  async function backupNow() {
    if (!credentials) {
      return;
    }
    const policyKey = trimToNull(backupNowPolicyKey);
    if (!policyKey) {
      updateOperationFeedback("backupNow", {
        busy: false,
        statusLine: "Choose a backup policy before requesting a backup.",
        result: null,
      });
      return;
    }
    await runOperation("backupNow", () =>
      requestProjectDeploymentBackupNow(credentials, tenantId, projectId, {
        backup_policy_key: policyKey,
      }),
    );
  }

  async function requestRestore() {
    if (!credentials) {
      return;
    }
    const resourceKey = trimToNull(restoreResourceKey);
    const backupKey = trimToNull(restoreBackupKey);
    if (!backupKey) {
      updateOperationFeedback("restore", {
        busy: false,
        statusLine: "Enter a backup policy key before requesting a restore.",
        result: null,
      });
      return;
    }
    await runOperation("restore", () =>
      requestProjectDeploymentRestore(credentials, tenantId, projectId, {
        backup_key: backupKey,
        resource_key: resourceKey,
        restore_mode: restoreMode,
        note: trimToNull(restoreNote),
      }),
    );
  }

  function operationPanel(title: string, key: OperationKey, description: string) {
    const feedback = operationFeedback[key];
    return (
      <div className="rounded-2xl border bg-background p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold">{title}</h3>
            <p className="mt-1 text-xs text-muted-foreground">{description}</p>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              if (key === "resources") void applyResources();
              if (key === "domains") void applyDomains();
              if (key === "backups") void applyBackups();
              if (key === "backupNow") void backupNow();
              if (key === "restore") void requestRestore();
            }}
            disabled={feedback.busy || savingConfig || deploying}
          >
            <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${feedback.busy ? "animate-spin" : ""}`} />
            {feedback.busy ? "Running…" : "Run"}
          </Button>
        </div>
        {feedback.statusLine ? (
          <div className="mt-3 rounded-lg border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
            {feedback.statusLine}
          </div>
        ) : null}
        {feedback.result ? (
          <div className="mt-3 space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={feedback.result.ok ? "success" : "warning"}>{feedback.result.ok ? "ok" : "needs attention"}</Badge>
              <Badge variant="outline">{feedback.result.action}</Badge>
              {feedback.result.job_id ? <Badge variant="outline">job {feedback.result.job_id}</Badge> : null}
              {feedback.result.status ? <Badge variant="outline">{feedback.result.status}</Badge> : null}
            </div>
            <pre className="max-h-44 overflow-auto rounded-xl border bg-muted/10 p-3 text-xs">
              {JSON.stringify(feedback.result.metadata ?? { details: feedback.result.details }, null, 2)}
            </pre>
          </div>
        ) : null}
      </div>
    );
  }

  async function createRelease() {
    if (!credentials) {
      return;
    }
    if (deploymentForm.source_strategy !== "dockerfile") {
      setStatusLine("Coolify release submission currently supports dockerfile deployments only.");
      return;
    }
    if (!deploymentForm.enabled) {
      setStatusLine("Enable deployment config before creating a release.");
      return;
    }
    if (!deploymentForm.environment_name.trim()) {
      setStatusLine("Set an environment name before creating a release.");
      return;
    }
    setDeploying(true);
    try {
      const payload: ProjectDeploymentReleaseCreatePayload = {
        git_ref: trimToNull(releaseDraft.git_ref),
        commit_sha: trimToNull(releaseDraft.commit_sha),
        reason: trimToNull(releaseDraft.reason),
      };
      const created = await createProjectDeploymentRelease(credentials, tenantId, projectId, payload);
      setReleases((current) => [created, ...current.filter((release) => release.release_id !== created.release_id)]);
      setExpandedReleaseId(created.release_id);
      setReleaseDraft(emptyReleaseDraft());
      setStatusLine(`Queued release ${created.release_id}.`);
    } catch (error) {
      setStatusLine(`Release failed: ${(error as Error).message}`);
    } finally {
      setDeploying(false);
    }
  }

  function updateDomain(index: number, updates: Partial<DeploymentDomainDraft>) {
    setDeploymentForm((current) => ({
      ...current,
      domains: current.domains.map((domain, currentIndex) => (currentIndex === index ? { ...domain, ...updates } : domain)),
    }));
  }

  function updateResource(index: number, updates: Partial<DeploymentResourceDraft>) {
    setDeploymentForm((current) => ({
      ...current,
      resources: current.resources.map((resource, currentIndex) => (currentIndex === index ? { ...resource, ...updates } : resource)),
    }));
  }

  function updateBackupPolicy(index: number, updates: Partial<DeploymentBackupDraft>) {
    setDeploymentForm((current) => ({
      ...current,
      backup_policies: current.backup_policies.map((policy, currentIndex) => (currentIndex === index ? { ...policy, ...updates } : policy)),
    }));
  }

  function removeDomain(index: number) {
    setDeploymentForm((current) => ({
      ...current,
      domains: current.domains.filter((_, currentIndex) => currentIndex !== index),
    }));
  }

  function removeResource(index: number) {
    setDeploymentForm((current) => {
      const removed = current.resources[index];
      const nextResources = current.resources.filter((_, currentIndex) => currentIndex !== index);
      const nextBackups = current.backup_policies.map((policy) =>
        policy.resource_key === removed?.key ? { ...policy, resource_key: nextResources[0]?.key ?? "" } : policy,
      );
      return {
        ...current,
        resources: nextResources,
        backup_policies: nextBackups,
      };
    });
  }

  function removeBackupPolicy(index: number) {
    setDeploymentForm((current) => ({
      ...current,
      backup_policies: current.backup_policies.filter((_, currentIndex) => currentIndex !== index),
    }));
  }

  const deployDisabledReason = !deploymentForm.enabled
    ? "Enable the deployment config first."
    : !deploymentForm.environment_name.trim()
      ? "Set an environment name before deploying."
      : deploymentForm.source_strategy !== "dockerfile"
        ? "This UI currently deploys dockerfile projects only."
        : null;

  return (
    <div className="space-y-6">
      {statusLine ? (
        <div className="rounded-xl border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
          {statusLine}
        </div>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-4">
        {[
          { label: "Releases", value: releaseCounts.total, note: `${releaseCounts.live} live` },
          { label: "Active", value: releaseCounts.active, note: "Provisioning or deploying" },
          { label: "Failed", value: releaseCounts.failed, note: "Needs attention" },
          { label: "Domains", value: deploymentForm.domains.filter((domain) => !isBlankDomainDraft(domain)).length, note: "Configured hosts" },
        ].map((item) => (
          <div key={item.label} className="rounded-2xl border bg-background px-4 py-3">
            <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">{item.label}</p>
            <p className="mt-1 text-2xl font-bold">{item.value}</p>
            <p className="mt-1 text-xs text-muted-foreground">{item.note}</p>
          </div>
        ))}
      </div>

      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b px-6 py-4">
          <div>
            <h2 className="text-base font-semibold">Deployment config</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Configure domains, resources, and backup policies before creating a release.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => void loadDeployment()} disabled={loading || savingConfig || deploying}>
              <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
              Refresh
            </Button>
            <Button variant="outline" size="sm" onClick={() => void saveDeploymentConfig()} disabled={savingConfig || deploying}>
              <Save className="mr-1.5 h-3.5 w-3.5" />
              {savingConfig ? "Saving…" : "Save config"}
            </Button>
          </div>
        </div>

        <div className="grid gap-4 p-6 lg:grid-cols-[minmax(0,360px)_minmax(0,1fr)]">
          <div className="space-y-4">
            <div className="rounded-2xl border bg-muted/20 p-4">
              <div className="space-y-3">
                <div className="space-y-1.5">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Enabled</label>
                  <div className="flex items-center gap-2">
                    <input
                      id="deployment-enabled"
                      type="checkbox"
                      checked={deploymentForm.enabled}
                      onChange={(event) => setDeploymentForm((current) => ({ ...current, enabled: event.target.checked }))}
                      className="h-4 w-4 rounded border-input"
                    />
                    <label htmlFor="deployment-enabled" className="text-sm">
                      Allow release creation
                    </label>
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
                  <p className="text-xs text-muted-foreground">
                    Release creation is currently wired for Dockerfile deployments only.
                  </p>
                </div>
              </div>
            </div>

            <div className="rounded-2xl border bg-muted/20 p-4">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <h3 className="text-sm font-semibold">Release</h3>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Trigger a deployment from a branch, tag, or commit. Leave fields empty to use project defaults.
                  </p>
                </div>
                <Badge variant={activeRelease ? deploymentStatusVariant(activeRelease.status) : "outline"} className="shrink-0">
                  {activeRelease ? activeRelease.status : "idle"}
                </Badge>
              </div>

              <div className="mt-4 space-y-3">
                <div className="grid gap-3">
                  <div className="space-y-1.5">
                    <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Git ref</label>
                    <Input
                      value={releaseDraft.git_ref}
                      onChange={(event) => setReleaseDraft((current) => ({ ...current, git_ref: event.target.value }))}
                      placeholder="main"
                      disabled={deploying}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Commit SHA</label>
                    <Input
                      value={releaseDraft.commit_sha}
                      onChange={(event) => setReleaseDraft((current) => ({ ...current, commit_sha: event.target.value }))}
                      placeholder="abcdef1234"
                      disabled={deploying}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Reason</label>
                    <Textarea
                      value={releaseDraft.reason}
                      onChange={(event) => setReleaseDraft((current) => ({ ...current, reason: event.target.value }))}
                      placeholder="Release for staging validation"
                      className="min-h-[88px]"
                      disabled={deploying}
                    />
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Button size="sm" onClick={() => void createRelease()} disabled={deploying || Boolean(deployDisabledReason)}>
                    <Plus className="mr-1.5 h-3.5 w-3.5" />
                    {deploying ? "Queuing…" : "Create release"}
                  </Button>
                  {deployDisabledReason ? (
                    <p className="text-xs text-muted-foreground">{deployDisabledReason}</p>
                  ) : null}
                </div>
              </div>
            </div>
          </div>

          <div className="space-y-4">
            <div className="rounded-2xl border bg-background">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3">
                <div>
                  <h3 className="text-sm font-semibold">Domains</h3>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Add the hostnames and paths that should be managed by the deployment target.
                  </p>
                </div>
                <Button variant="outline" size="sm" onClick={appendDomain} disabled={savingConfig || deploying}>
                  <Plus className="mr-1.5 h-3.5 w-3.5" />
                  Add domain
                </Button>
              </div>
              <div className="space-y-3 p-4">
                {deploymentForm.domains.length === 0 ? (
                  <p className="rounded-lg border border-dashed px-4 py-6 text-sm text-muted-foreground">
                    No domains configured yet.
                  </p>
                ) : (
                  deploymentForm.domains.map((domain, index) => (
                    <div key={`${domain.key || "domain"}-${index}`} className="grid gap-3 rounded-xl border p-4 lg:grid-cols-[1fr_1.4fr_1fr_auto]">
                      <div className="space-y-1.5">
                        <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Key</label>
                        <Input
                          value={domain.key}
                          onChange={(event) => updateDomain(index, { key: event.target.value })}
                          placeholder="main"
                          disabled={savingConfig || deploying}
                        />
                      </div>
                      <div className="space-y-1.5">
                        <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Host</label>
                        <Input
                          value={domain.host}
                          onChange={(event) => updateDomain(index, { host: event.target.value })}
                          placeholder="app.example.com"
                          disabled={savingConfig || deploying}
                        />
                      </div>
                      <div className="space-y-1.5">
                        <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Path</label>
                        <Input
                          value={domain.path}
                          onChange={(event) => updateDomain(index, { path: event.target.value })}
                          placeholder="/"
                          disabled={savingConfig || deploying}
                        />
                        <div className="flex flex-wrap gap-3 text-sm">
                          <label className="inline-flex items-center gap-2">
                            <input
                              type="checkbox"
                              checked={domain.is_primary}
                              onChange={(event) => updateDomain(index, { is_primary: event.target.checked })}
                              className="h-4 w-4 rounded border-input"
                              disabled={savingConfig || deploying}
                            />
                            Primary
                          </label>
                          <label className="inline-flex items-center gap-2">
                            <input
                              type="checkbox"
                              checked={domain.tls_enabled}
                              onChange={(event) => updateDomain(index, { tls_enabled: event.target.checked })}
                              className="h-4 w-4 rounded border-input"
                              disabled={savingConfig || deploying}
                            />
                            TLS
                          </label>
                        </div>
                      </div>
                      <div className="flex items-start justify-end">
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="h-10 w-10 px-0"
                          onClick={() => removeDomain(index)}
                          disabled={savingConfig || deploying}
                          aria-label="Remove domain"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>

            <div className="rounded-2xl border bg-background">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3">
                <div>
                  <h3 className="text-sm font-semibold">Resources</h3>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Track databases, caches, object storage, and persistent volumes for the deployment.
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  {RESOURCE_KIND_OPTIONS.map((option) => (
                    <Button
                      key={option.value}
                      variant="outline"
                      size="sm"
                      onClick={() => appendResource(option.value)}
                      disabled={savingConfig || deploying}
                    >
                      <Plus className="mr-1.5 h-3.5 w-3.5" />
                      {option.label}
                    </Button>
                  ))}
                </div>
              </div>
              <div className="space-y-3 p-4">
                {deploymentForm.resources.length === 0 ? (
                  <p className="rounded-lg border border-dashed px-4 py-6 text-sm text-muted-foreground">
                    No deployment resources configured yet.
                  </p>
                ) : (
                  deploymentForm.resources.map((resource, index) => (
                    <div key={`${resource.key || "resource"}-${index}`} className="grid gap-3 rounded-xl border p-4 xl:grid-cols-[1fr_1fr_1.3fr_auto]">
                      <div className="space-y-1.5">
                        <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Key</label>
                        <Input
                          value={resource.key}
                          onChange={(event) => updateResource(index, { key: event.target.value })}
                          placeholder="postgres"
                          disabled={savingConfig || deploying}
                        />
                      </div>
                      <div className="space-y-1.5">
                        <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Kind</label>
                        <select
                          className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                          value={resource.kind}
                          onChange={(event) => updateResource(index, { kind: event.target.value })}
                          disabled={savingConfig || deploying}
                        >
                          <option value="">Select kind</option>
                          {RESOURCE_KIND_OPTIONS.map((option) => (
                            <option key={option.value} value={option.value}>
                              {option.label}
                            </option>
                          ))}
                        </select>
                      </div>
                      <div className="space-y-1.5">
                        <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Name</label>
                        <Input
                          value={resource.name}
                          onChange={(event) => updateResource(index, { name: event.target.value })}
                          placeholder="Primary database"
                          disabled={savingConfig || deploying}
                        />
                        <Textarea
                          value={resource.configText}
                          onChange={(event) => updateResource(index, { configText: event.target.value })}
                          className="min-h-[90px] font-mono text-xs"
                          placeholder='{"database":"app"}'
                          disabled={savingConfig || deploying}
                        />
                      </div>
                      <div className="flex items-start justify-end">
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="h-10 w-10 px-0"
                          onClick={() => removeResource(index)}
                          disabled={savingConfig || deploying}
                          aria-label="Remove resource"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>

            <div className="rounded-2xl border bg-background">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3">
                <div>
                  <h3 className="text-sm font-semibold">Backup policies</h3>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Pair backup schedules with the resources they should protect.
                  </p>
                </div>
                <Button variant="outline" size="sm" onClick={appendBackupPolicy} disabled={savingConfig || deploying || deploymentForm.resources.length === 0}>
                  <Plus className="mr-1.5 h-4 w-4" />
                  Add policy
                </Button>
              </div>
              <div className="space-y-3 p-4">
                {deploymentForm.backup_policies.length === 0 ? (
                  <p className="rounded-lg border border-dashed px-4 py-6 text-sm text-muted-foreground">
                    No backup policies configured yet.
                  </p>
                ) : (
                  deploymentForm.backup_policies.map((backupPolicy, index) => (
                    <div key={`${backupPolicy.key || "backup"}-${index}`} className="grid gap-3 rounded-xl border p-4 xl:grid-cols-[1fr_1fr_1fr_1fr_auto]">
                      <div className="space-y-1.5">
                        <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Key</label>
                        <Input
                          value={backupPolicy.key}
                          onChange={(event) => updateBackupPolicy(index, { key: event.target.value })}
                          placeholder="daily-db"
                          disabled={savingConfig || deploying}
                        />
                      </div>
                      <div className="space-y-1.5">
                        <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Resource</label>
                        <select
                          className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                          value={backupPolicy.resource_key}
                          onChange={(event) => updateBackupPolicy(index, { resource_key: event.target.value })}
                          disabled={savingConfig || deploying || deploymentForm.resources.length === 0}
                        >
                          <option value="">Choose resource</option>
                          {deploymentForm.resources
                            .filter((resource) => !isBlankResourceDraft(resource))
                            .map((resource) => (
                              <option key={resource.key} value={resource.key.trim().toLowerCase()}>
                                {resource.key || resource.name || "resource"}
                              </option>
                            ))}
                        </select>
                      </div>
                      <div className="space-y-1.5">
                        <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Schedule</label>
                        <Input
                          value={backupPolicy.schedule}
                          onChange={(event) => updateBackupPolicy(index, { schedule: event.target.value })}
                          placeholder="0 2 * * *"
                          disabled={savingConfig || deploying}
                        />
                        <Input
                          value={backupPolicy.retention_days}
                          onChange={(event) =>
                            updateBackupPolicy(index, { retention_days: normalizeBackupRetention(event.target.value) })
                          }
                          placeholder="7"
                          type="number"
                          min={1}
                          disabled={savingConfig || deploying}
                        />
                      </div>
                      <div className="space-y-1.5">
                        <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Config JSON</label>
                        <Textarea
                          value={backupPolicy.configText}
                          onChange={(event) => updateBackupPolicy(index, { configText: event.target.value })}
                          className="min-h-[90px] font-mono text-xs"
                          placeholder='{"target":"s3"}'
                          disabled={savingConfig || deploying}
                        />
                        <label className="inline-flex items-center gap-2 text-sm">
                          <input
                            type="checkbox"
                            checked={backupPolicy.enabled}
                            onChange={(event) => updateBackupPolicy(index, { enabled: event.target.checked })}
                            className="h-4 w-4 rounded border-input"
                            disabled={savingConfig || deploying}
                          />
                          Enabled
                        </label>
                      </div>
                      <div className="flex items-start justify-end">
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="h-10 w-10 px-0"
                          onClick={() => removeBackupPolicy(index)}
                          disabled={savingConfig || deploying}
                          aria-label="Remove backup policy"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b px-6 py-4">
          <div className="flex items-center gap-2">
            <CircleAlert className="h-4 w-4 text-muted-foreground" />
            <div>
              <h2 className="text-base font-semibold">Orchestration actions</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                Apply the saved deployment config to the runtime and request backup or restore operations.
              </p>
            </div>
          </div>
          <Button variant="ghost" size="sm" onClick={() => void loadDeployment()} disabled={loading || savingConfig || deploying}>
            <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            Refresh state
          </Button>
        </div>

        <div className="grid gap-4 p-6 lg:grid-cols-2">
          <div className="space-y-4">
            {operationPanel(
              "Apply resources",
              "resources",
              "Push resource definitions so the runtime can provision databases, caches, storage, and volumes.",
            )}
            {operationPanel(
              "Apply domains / TLS",
              "domains",
              "Sync the configured hostnames and TLS routing for this project.",
            )}
            {operationPanel(
              "Apply backups",
              "backups",
              "Push backup schedules and retention settings for the configured resources.",
            )}
          </div>

          <div className="space-y-4">
            <div className="rounded-2xl border bg-background p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold">Backup now</h3>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Trigger an on-demand backup for a resource or backup policy.
                  </p>
                </div>
                <Button variant="outline" size="sm" onClick={() => void backupNow()} disabled={operationFeedback.backupNow.busy || savingConfig || deploying}>
                  <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${operationFeedback.backupNow.busy ? "animate-spin" : ""}`} />
                  {operationFeedback.backupNow.busy ? "Running…" : "Run"}
                </Button>
              </div>
              <div className="mt-4 grid gap-3 md:grid-cols-2">
                <div className="space-y-1.5">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Resource</label>
                  <select
                    className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                    value={backupNowResourceKey}
                    onChange={(event) => setBackupNowResourceKey(event.target.value)}
                    disabled={operationFeedback.backupNow.busy || savingConfig || deploying}
                  >
                    <option value="">Choose resource</option>
                    {deploymentForm.resources
                      .filter((resource) => !isBlankResourceDraft(resource))
                      .map((resource) => (
                        <option key={resource.key} value={resource.key.trim().toLowerCase()}>
                          {resource.key || resource.name || "resource"}
                        </option>
                      ))}
                  </select>
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Backup policy</label>
                  <select
                    className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                    value={backupNowPolicyKey}
                    onChange={(event) => setBackupNowPolicyKey(event.target.value)}
                    disabled={operationFeedback.backupNow.busy || savingConfig || deploying}
                  >
                    <option value="">Choose policy</option>
                    {deploymentForm.backup_policies
                      .filter((policy) => !isBlankBackupDraft(policy))
                      .map((policy) => (
                        <option key={policy.key} value={policy.key.trim().toLowerCase()}>
                          {policy.key || policy.schedule || "policy"}
                        </option>
                      ))}
                  </select>
                </div>
              </div>
              {operationFeedback.backupNow.statusLine ? (
                <div className="mt-3 rounded-lg border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                  {operationFeedback.backupNow.statusLine}
                </div>
              ) : null}
              {operationFeedback.backupNow.result ? (
                <pre className="mt-3 max-h-40 overflow-auto rounded-xl border bg-muted/10 p-3 text-xs">
                  {JSON.stringify(operationFeedback.backupNow.result.metadata ?? { details: operationFeedback.backupNow.result.details }, null, 2)}
                </pre>
              ) : null}
            </div>

            <div className="rounded-2xl border bg-background p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold">Restore request</h3>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Ask the backend to restore a named backup for a selected resource.
                  </p>
                </div>
                <Button variant="outline" size="sm" onClick={() => void requestRestore()} disabled={operationFeedback.restore.busy || savingConfig || deploying}>
                  <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${operationFeedback.restore.busy ? "animate-spin" : ""}`} />
                  {operationFeedback.restore.busy ? "Running…" : "Run"}
                </Button>
              </div>
              <div className="mt-4 grid gap-3 md:grid-cols-2">
                <div className="space-y-1.5">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Resource</label>
                  <select
                    className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                    value={restoreResourceKey}
                    onChange={(event) => setRestoreResourceKey(event.target.value)}
                    disabled={operationFeedback.restore.busy || savingConfig || deploying}
                  >
                    <option value="">Choose resource</option>
                    {deploymentForm.resources
                      .filter((resource) => !isBlankResourceDraft(resource))
                      .map((resource) => (
                        <option key={resource.key} value={resource.key.trim().toLowerCase()}>
                          {resource.key || resource.name || "resource"}
                        </option>
                      ))}
                  </select>
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Backup key</label>
                  <Input
                    value={restoreBackupKey}
                    onChange={(event) => setRestoreBackupKey(event.target.value)}
                    placeholder="daily-db-2026-04-09"
                    disabled={operationFeedback.restore.busy || savingConfig || deploying}
                  />
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Restore mode</label>
                  <select
                    className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                    value={restoreMode}
                    onChange={(event) => setRestoreMode(event.target.value as "replace" | "clone")}
                    disabled={operationFeedback.restore.busy || savingConfig || deploying}
                  >
                    <option value="replace">Replace current</option>
                    <option value="clone">Clone new target</option>
                  </select>
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Note</label>
                  <Textarea
                    value={restoreNote}
                    onChange={(event) => setRestoreNote(event.target.value)}
                    placeholder="Operator requested restore"
                    className="min-h-[88px]"
                    disabled={operationFeedback.restore.busy || savingConfig || deploying}
                  />
                </div>
              </div>
              {operationFeedback.restore.statusLine ? (
                <div className="mt-3 rounded-lg border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                  {operationFeedback.restore.statusLine}
                </div>
              ) : null}
              {operationFeedback.restore.result ? (
                <pre className="mt-3 max-h-40 overflow-auto rounded-xl border bg-muted/10 p-3 text-xs">
                  {JSON.stringify(operationFeedback.restore.result.metadata ?? { details: operationFeedback.restore.result.details }, null, 2)}
                </pre>
              ) : null}
            </div>
          </div>
        </div>
      </div>

      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b px-6 py-4">
          <div>
            <h2 className="text-base font-semibold">Release history</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Track the current status of submitted releases and inspect the deployment snapshot.
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={() => void loadDeployment()} disabled={loading || savingConfig || deploying}>
            <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            Reload
          </Button>
        </div>

        {releases.length === 0 ? (
          <div className="px-6 py-12 text-center text-sm text-muted-foreground">
            No releases have been created for this project yet.
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="pl-6">Release</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Requested</TableHead>
                <TableHead>Provider</TableHead>
                <TableHead>Summary</TableHead>
                <TableHead className="pr-6 text-right">Details</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {releases.map((release) => {
                const expanded = expandedReleaseId === release.release_id;
                return (
                  <Fragment key={release.release_id}>
                    <TableRow className={cn("border-l-2", release.status === "failed" ? "border-l-destructive" : "border-l-transparent")}>
                      <TableCell className="pl-6">
                        <div className="font-medium">{release.release_id}</div>
                        <div className="text-xs text-muted-foreground">{release.environment_name ?? "default environment"}</div>
                      </TableCell>
                      <TableCell>
                        <Badge variant={deploymentStatusVariant(release.status)}>{release.status}</Badge>
                        {release.last_error ? (
                          <p className="mt-1 max-w-[280px] truncate text-xs text-destructive" title={release.last_error}>
                            {release.last_error}
                          </p>
                        ) : null}
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        <div>{formatTimestamp(release.requested_at)}</div>
                        <div className="text-xs">{release.requested_by_user_id ?? "system"}</div>
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        <div>{release.provider}</div>
                        <div className="text-xs">{release.source_strategy ?? "—"}</div>
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        {deploymentReleaseSummary(release)}
                        <div className="mt-1 flex flex-wrap gap-1">
                          {release.provider_context?.deployment_uuid ? (
                            <Badge variant="outline" className="text-[10px]">
                              deployment {String(release.provider_context.deployment_uuid).slice(0, 8)}
                            </Badge>
                          ) : null}
                          {release.provider_context?.application_uuid ? (
                            <Badge variant="outline" className="text-[10px]">
                              app {String(release.provider_context.application_uuid).slice(0, 8)}
                            </Badge>
                          ) : null}
                        </div>
                      </TableCell>
                      <TableCell className="pr-6 text-right">
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => setExpandedReleaseId((current) => (current === release.release_id ? null : release.release_id))}
                        >
                          {expanded ? <ChevronUp className="mr-1.5 h-3.5 w-3.5" /> : <ChevronDown className="mr-1.5 h-3.5 w-3.5" />}
                          {expanded ? "Hide" : "Show"}
                        </Button>
                      </TableCell>
                    </TableRow>
                    {expanded ? (
                      <TableRow key={`${release.release_id}-details`}>
                      <TableCell colSpan={6} className="bg-muted/20 px-6 py-4">
                          <div className="grid gap-4 lg:grid-cols-2">
                            <div className="space-y-2">
                              <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Deployment snapshot</p>
                              <pre className="max-h-72 overflow-auto rounded-xl border bg-background p-3 text-xs">
                                {JSON.stringify(release.deployment_snapshot ?? {}, null, 2)}
                              </pre>
                            </div>
                            <div className="space-y-2">
                              <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Provider context</p>
                              <pre className="max-h-72 overflow-auto rounded-xl border bg-background p-3 text-xs">
                                {JSON.stringify(release.provider_context ?? {}, null, 2)}
                              </pre>
                            </div>
                          </div>
                          <div className="mt-4 grid gap-3 text-sm sm:grid-cols-3">
                            <div>
                              <p className="text-xs uppercase tracking-wide text-muted-foreground">Started</p>
                              <p>{formatTimestamp(release.started_at)}</p>
                            </div>
                            <div>
                              <p className="text-xs uppercase tracking-wide text-muted-foreground">Completed</p>
                              <p>{formatTimestamp(release.completed_at)}</p>
                            </div>
                            <div>
                              <p className="text-xs uppercase tracking-wide text-muted-foreground">Updated</p>
                              <p>{formatTimestamp(release.updated_at)}</p>
                            </div>
                          </div>
                        </TableCell>
                      </TableRow>
                    ) : null}
                  </Fragment>
                );
              })}
            </TableBody>
          </Table>
        )}
      </div>

      {canOverrideStatus ? (
        <div className="rounded-xl border border-dashed px-4 py-3 text-xs text-muted-foreground">
          Platform admin context detected. Release status overrides are available in the backend API for reconciliation tools.
        </div>
      ) : null}
    </div>
  );
}
