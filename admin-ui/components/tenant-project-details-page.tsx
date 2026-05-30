"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useParams, usePathname, useRouter } from "next/navigation";
import {
  Archive,
  ExternalLink,
  KeyRound,
  Pencil,
  RefreshCw,
  Save,
  SlidersHorizontal,
  X,
  Trash2,
} from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { ProjectParentWorkBoard } from "@/components/project-parent-work-board";
import { ProjectSectionTabs } from "@/components/project-section-tabs";
import { ProjectAutomationsContent, ProjectNotificationsContent } from "@/components/tenant-project-discord-page";
import { CodexModelSelect } from "@/components/codex-model-select";
import { OverrideSegmentedControl } from "@/components/override-segmented-control";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/components/ui/toast-provider";
import { formatTimestamp } from "@/lib/datetime";
import {
  createArchitectureDocument,
  getProject,
  listArchitectureDocuments,
  listConfluencePages,
  listConfluenceSpaces,
  getTenant,
  listCodexModels,
  listGitHubRepositories,
  listJiraProjects,
  listRuns,
  listWebhookQueueJobs,
  retryWebhookJob,
  resolveProjectJiraRunBoard,
  RUN_STATUSES,
  updateProjectArchiveState,
  updateProjectConfiguration,
  updateProjectPolicy,
  updateProjectSecretRefs,
  type ArchitectureDocumentRecord,
  type ConfluencePageRecord,
  type ConfluenceSpaceRecord,
  type ProjectArchitectureDocsConfig,
  type ProjectRecord,
  type RunRecord,
  type RunStatus,
  type WebhookQueueJobRecord,
  type WebhookQueueSummaryRecord,
} from "@/lib/api";
import {
  canAccessPlatformAdmin,
  canAccessTechnicalSurface,
  canManageProjects,
  getProjectArchiveRedirectRoute,
} from "@/lib/auth-routing";
import { buildRunDetailPath, resolveProjectSection, type ProjectSection } from "@/lib/dashboard-paths";

type Tab = Exclude<ProjectSection, "knowledge">;
type SettingsSection = "general" | "ai" | "automation" | "knowledge" | "governance";
type OverrideToggleValue = "inherit" | "enabled" | "disabled";
type RequireAgentsValue = "inherit" | "required";
type KnowledgeModeValue = "inherit" | "safe" | "balanced" | "aggressive";
type AllowedCommandsMode = "inherit" | "custom";

type ProjectFormState = {
  name: string;
  github_repository: string;
  jira_project_key: string;
  architecture_provider: "" | "internal" | "confluence";
  architecture_space_key: string;
  architecture_parent_page_id: string;
  codex_model: string | null;
  codex_reasoning_effort: "low" | "medium" | "high" | null;
  allow_jira_transitions: OverrideToggleValue;
  allow_pr_creation: OverrideToggleValue;
  allow_code_reviews: OverrideToggleValue;
  allow_pr_remediation: OverrideToggleValue;
  allow_manual_pr_fix_requests: OverrideToggleValue;
  allow_label_mutations: OverrideToggleValue;
  allow_auto_merge: OverrideToggleValue;
  staging_merge_check_enabled: OverrideToggleValue;
  staging_branch: string;
  require_agents_md: RequireAgentsValue;
  knowledge_base_enabled: OverrideToggleValue;
  knowledge_auto_answer_mode: KnowledgeModeValue;
  max_dev_test_review_loops: string;
  max_pr_auto_remediation_loops: string;
  max_concurrent_runs: string;
  allowed_commands_mode: AllowedCommandsMode;
  allowed_commands_text: string;
};

const SETTINGS_SECTIONS: { id: SettingsSection; label: string }[] = [
  { id: "general", label: "General" },
  { id: "ai", label: "AI" },
  { id: "automation", label: "Automation" },
  { id: "knowledge", label: "Knowledge" },
  { id: "governance", label: "Governance" },
];

const STATUS_BORDER: Record<string, string> = {
  succeeded: "border-l-success",
  failed: "border-l-destructive",
  blocked: "border-l-warning",
  running: "border-l-info",
  pending: "border-l-muted-foreground",
};

const BOOLEAN_OVERRIDE_OPTIONS: { value: OverrideToggleValue; label: string }[] = [
  { value: "inherit", label: "Inherit" },
  { value: "enabled", label: "On" },
  { value: "disabled", label: "Off" },
];

function booleanOverrideToState(value: unknown): OverrideToggleValue {
  if (value === true) {
    return "enabled";
  }
  if (value === false) {
    return "disabled";
  }
  return "inherit";
}

function booleanStateToOverride(value: OverrideToggleValue): boolean | undefined {
  if (value === "enabled") {
    return true;
  }
  if (value === "disabled") {
    return false;
  }
  return undefined;
}

function requireAgentsOverrideToState(value: unknown): RequireAgentsValue {
  return value === true ? "required" : "inherit";
}

function knowledgeModeOverrideToState(value: unknown): KnowledgeModeValue {
  const normalized = String(value || "").trim().toLowerCase();
  if (normalized === "safe" || normalized === "balanced" || normalized === "aggressive") {
    return normalized;
  }
  return "inherit";
}

function numericOverrideToText(value: unknown): string {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    return String(value);
  }
  const normalized = String(value || "").trim();
  return normalized;
}

function buildProjectFormState(payload: ProjectRecord | null): ProjectFormState {
  const overrides = payload?.policy_overrides ?? {};
  const allowedCommands = Array.isArray(overrides.allowed_commands)
    ? overrides.allowed_commands.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
  return {
    name: payload?.name ?? "",
    github_repository: payload?.github_repository ?? "",
    jira_project_key: payload?.jira_project_key ?? "",
    architecture_provider:
      payload?.architecture_docs?.provider === "internal" || payload?.architecture_docs?.provider === "confluence"
        ? payload.architecture_docs.provider
        : "",
    architecture_space_key: payload?.architecture_docs?.space_key ?? "",
    architecture_parent_page_id: payload?.architecture_docs?.parent_page_id ?? "",
    codex_model: typeof overrides.codex_model === "string" ? overrides.codex_model : null,
    codex_reasoning_effort:
      overrides.codex_reasoning_effort === "low" ||
      overrides.codex_reasoning_effort === "medium" ||
      overrides.codex_reasoning_effort === "high"
        ? overrides.codex_reasoning_effort
        : null,
    allow_jira_transitions: booleanOverrideToState(overrides.allow_jira_transitions),
    allow_pr_creation: booleanOverrideToState(overrides.allow_pr_creation),
    allow_code_reviews: booleanOverrideToState(overrides.allow_code_reviews),
    allow_pr_remediation: booleanOverrideToState(overrides.allow_pr_remediation),
    allow_manual_pr_fix_requests: booleanOverrideToState(overrides.allow_manual_pr_fix_requests),
    allow_label_mutations: booleanOverrideToState(overrides.allow_label_mutations),
    allow_auto_merge: booleanOverrideToState(overrides.allow_auto_merge),
    staging_merge_check_enabled: booleanOverrideToState(overrides.staging_admission_enabled),
    staging_branch: typeof overrides.staging_branch === "string" ? overrides.staging_branch : "",
    require_agents_md: requireAgentsOverrideToState(overrides.require_agents_md),
    knowledge_base_enabled: booleanOverrideToState(overrides.knowledge_base_enabled),
    knowledge_auto_answer_mode: knowledgeModeOverrideToState(overrides.knowledge_auto_answer_mode),
    max_dev_test_review_loops: numericOverrideToText(overrides.max_dev_test_review_loops),
    max_pr_auto_remediation_loops: numericOverrideToText(overrides.max_pr_auto_remediation_loops),
    max_concurrent_runs: numericOverrideToText(overrides.max_concurrent_runs),
    allowed_commands_mode: "allowed_commands" in overrides ? "custom" : "inherit",
    allowed_commands_text: allowedCommands.join("\n"),
  };
}

function parsePositiveOverride(value: string): number | undefined {
  const normalized = value.trim();
  if (!normalized) {
    return undefined;
  }
  const parsed = Number(normalized);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return undefined;
  }
  return Math.trunc(parsed);
}

function formatBoolean(value: boolean): string {
  return value ? "Enabled" : "Disabled";
}

function buildArchitectureDocsPayload(form: ProjectFormState): ProjectArchitectureDocsConfig | null {
  if (!form.architecture_provider) {
    return null;
  }
  if (form.architecture_provider === "internal") {
    return {
      provider: "internal",
    };
  }
  return {
    provider: form.architecture_provider,
    space_key: form.architecture_space_key.trim() || null,
    parent_page_id: form.architecture_parent_page_id.trim() || null
  };
}

export function TenantProjectDetailsPage() {
  const params = useParams<{ tenantId: string; projectId: string }>();
  const pathname = usePathname();
  const router = useRouter();
  const { credentials, ready, principal } = useAuth();
  const { showToast } = useToast();

  // Project state
  const [project, setProject] = useState<ProjectRecord | null>(null);
  const [form, setForm] = useState<ProjectFormState>(() => buildProjectFormState(null));
  const [busy, setBusy] = useState(false);
  const [archiveConfirmationName, setArchiveConfirmationName] = useState("");
  const [showArchiveConfirm, setShowArchiveConfirm] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [repoOptions, setRepoOptions] = useState<string[]>([]);
  const [jiraOptions, setJiraOptions] = useState<string[]>([]);
  const [confluenceSpaces, setConfluenceSpaces] = useState<ConfluenceSpaceRecord[]>([]);
  const [confluencePages, setConfluencePages] = useState<ConfluencePageRecord[]>([]);
  const [confluenceSpacesLoading, setConfluenceSpacesLoading] = useState(false);
  const [confluencePagesLoading, setConfluencePagesLoading] = useState(false);
  const [confluenceCreateSpaceUrl, setConfluenceCreateSpaceUrl] = useState<string | null>(null);
  const [confluenceStatusLine, setConfluenceStatusLine] = useState("");
  const [codexModels, setCodexModels] = useState<{ id: string; label: string; description?: string | null }[]>([]);
  const [globalCodexModel, setGlobalCodexModel] = useState("");
  const [reasoningEfforts, setReasoningEfforts] = useState<{ id: string; label: string; description?: string | null }[]>([]);
  const [globalCodexReasoningEffort, setGlobalCodexReasoningEffort] = useState("");

  // Runs state
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [runsStatusLine, setRunsStatusLine] = useState("");
  const [runsBusy, setRunsBusy] = useState(false);
  const [webhookJobs, setWebhookJobs] = useState<WebhookQueueJobRecord[]>([]);
  const [webhookSummary, setWebhookSummary] = useState<WebhookQueueSummaryRecord | null>(null);
  const [webhookBusy, setWebhookBusy] = useState(false);
  const [webhookStatusLine, setWebhookStatusLine] = useState("");
  const [retryingWebhookJobId, setRetryingWebhookJobId] = useState<string | null>(null);
  const [webhookQueryFilter, setWebhookQueryFilter] = useState("");
  const [webhookStatusFilter, setWebhookStatusFilter] = useState<"all" | string>("all");
  const [webhookTransportFilter, setWebhookTransportFilter] = useState<"all" | string>("all");
  const [webhookTotal, setWebhookTotal] = useState(0);
  const [webhookPage, setWebhookPage] = useState(1);
  const [webhookPageSize, setWebhookPageSize] = useState<25 | 50 | 100>(25);
  const [selectedWebhookJobId, setSelectedWebhookJobId] = useState<string | null>(null);
  const [activeSettingsSection, setActiveSettingsSection] = useState<SettingsSection>("general");
  const [runIssueFilter, setRunIssueFilter] = useState("");
  const [runStatusFilter, setRunStatusFilter] = useState<RunStatus | "all">("all");
  const [runPrFilter, setRunPrFilter] = useState<"any" | "none" | "has_value">("any");
  const [runFromDate, setRunFromDate] = useState("");
  const [runToDate, setRunToDate] = useState("");
  const [runPage, setRunPage] = useState(1);
  const [runPageSize, setRunPageSize] = useState<25 | 50 | 100>(25);
  // Secrets state
  const [secretRefs, setSecretRefs] = useState<Record<string, string>>({});
  const [secretKey, setSecretKey] = useState("");
  const [secretValue, setSecretValue] = useState("");
  const [editingSecretKey, setEditingSecretKey] = useState<string | null>(null);
  const [secretsBusy, setSecretsBusy] = useState(false);
  const [secretsStatusLine, setSecretsStatusLine] = useState("");
  const projectSecretPrefix = useMemo(
    () => `project/${params.tenantId}/${params.projectId}/`,
    [params.tenantId, params.projectId]
  );
  const canReadCodexModels = canAccessPlatformAdmin(principal);
  const isPlatformSuperAdmin = canAccessPlatformAdmin(principal);
  const allowProjectManagement = canManageProjects(principal, params.tenantId);
  const canAccessTechnicalPolicy = canAccessTechnicalSurface(principal, params.tenantId);
  const activeTab = useMemo<Tab>(() => {
    const resolved = resolveProjectSection(pathname) ?? "overview";
    if (resolved === "knowledge") {
      return "overview";
    }
    if (resolved === "runs" && !isPlatformSuperAdmin) {
      return "overview";
    }
    if (allowProjectManagement) {
      return resolved;
    }
    return resolved === "settings" || resolved === "notifications" || resolved === "automations" || resolved === "secrets" || resolved === "danger"
      ? "overview"
      : resolved;
  }, [allowProjectManagement, isPlatformSuperAdmin, pathname]);
  const webhookStatusOptions = useMemo(
    () => Array.from(new Set(webhookJobs.map((job) => job.status).filter(Boolean))).sort(),
    [webhookJobs],
  );
  const webhookTransportOptions = useMemo(
    () => Array.from(new Set(webhookJobs.map((job) => job.transport).filter(Boolean))).sort(),
    [webhookJobs],
  );
  const selectedWebhookJob = useMemo(
    () => webhookJobs.find((job) => job.job_id === selectedWebhookJobId) ?? null,
    [selectedWebhookJobId, webhookJobs],
  );

  async function loadOptions() {
    if (!credentials) return;
    if (!allowProjectManagement) {
      setRepoOptions([]);
      setJiraOptions([]);
      return;
    }
    try {
      const tenant = await getTenant(credentials, params.tenantId);
      if (canReadCodexModels) {
        try {
          const modelCatalog = await listCodexModels(credentials, { profileName: "engineering_execution" });
          setCodexModels(modelCatalog.models);
          setGlobalCodexModel(modelCatalog.default_model);
          setReasoningEfforts(modelCatalog.reasoning_efforts);
          setGlobalCodexReasoningEffort(modelCatalog.default_reasoning_effort);
        } catch {
          setCodexModels([]);
          setGlobalCodexModel("");
          setReasoningEfforts([]);
          setGlobalCodexReasoningEffort("");
        }
      } else {
        setCodexModels([]);
        setGlobalCodexModel("");
        setReasoningEfforts([]);
        setGlobalCodexReasoningEffort("");
      }
      if (tenant.github.installation_id) {
        const repos = await listGitHubRepositories(credentials, params.tenantId);
        setRepoOptions(repos.map((repo) => repo.html_url));
      } else {
        setRepoOptions([]);
      }
      if (tenant.jira.connection_id) {
        const jiraProjects = await listJiraProjects(credentials, tenant.jira.connection_id);
        setJiraOptions(jiraProjects.map((p) => p.key));
      } else {
        setJiraOptions([]);
      }
    } catch {
      setRepoOptions([]);
      setJiraOptions([]);
    }
  }

  async function loadConfluenceSpaces() {
    if (!credentials || !allowProjectManagement) {
      setConfluenceSpaces([]);
      setConfluenceCreateSpaceUrl(null);
      setConfluenceStatusLine("");
      return;
    }
    setConfluenceSpacesLoading(true);
    try {
      const payload = await listConfluenceSpaces(credentials, params.tenantId);
      setConfluenceSpaces(payload.items);
      setConfluenceCreateSpaceUrl(payload.create_space_url);
      setConfluenceStatusLine("");
    } catch (error) {
      setConfluenceSpaces([]);
      setConfluenceCreateSpaceUrl(null);
      setConfluenceStatusLine(`Confluence spaces are unavailable: ${(error as Error).message}`);
    } finally {
      setConfluenceSpacesLoading(false);
    }
  }

  async function loadConfluencePages(spaceKey: string) {
    const normalizedSpaceKey = spaceKey.trim();
    if (!credentials || !allowProjectManagement || !normalizedSpaceKey) {
      setConfluencePages([]);
      setConfluenceStatusLine("");
      return;
    }
    setConfluencePagesLoading(true);
    try {
      const pages = await listConfluencePages(
        credentials,
        params.tenantId,
        normalizedSpaceKey,
        form.architecture_parent_page_id.trim() || null,
      );
      setConfluencePages(pages);
      setConfluenceStatusLine("");
    } catch (error) {
      setConfluencePages([]);
      setConfluenceStatusLine(`Confluence pages are unavailable: ${(error as Error).message}`);
    } finally {
      setConfluencePagesLoading(false);
    }
  }

  async function loadRuns() {
    if (!credentials) return;
    setRunsBusy(true);
    try {
      const from = runFromDate ? new Date(`${runFromDate}T00:00:00.000Z`).toISOString() : undefined;
      const to = runToDate ? new Date(`${runToDate}T23:59:59.999Z`).toISOString() : undefined;
      const payload = await listRuns(credentials, {
        tenantId: params.tenantId,
        projectId: params.projectId,
        issue: runIssueFilter || undefined,
        status: runStatusFilter === "all" ? undefined : runStatusFilter,
        prState: runPrFilter === "any" ? undefined : runPrFilter,
        from,
        to,
        limit: runPageSize,
        offset: (runPage - 1) * runPageSize,
      });
      setRuns(payload);
      setRunsStatusLine("");
    } catch (error) {
      setRuns([]);
      setRunsStatusLine(`Runs are unavailable: ${(error as Error).message}`);
    } finally {
      setRunsBusy(false);
    }
  }

  async function loadWebhookJobs() {
    if (!credentials) return;
    setWebhookBusy(true);
    try {
      const payload = await listWebhookQueueJobs(credentials, {
        tenantId: params.tenantId,
        projectId: params.projectId,
        status: webhookStatusFilter === "all" ? undefined : webhookStatusFilter,
        transport: webhookTransportFilter === "all" ? undefined : webhookTransportFilter,
        subjectKey: webhookQueryFilter.trim() || undefined,
        limit: webhookPageSize,
        offset: (webhookPage - 1) * webhookPageSize,
      });
      setWebhookJobs(payload.items);
      setSelectedWebhookJobId((current) => (
        current && payload.items.some((job) => job.job_id === current) ? current : null
      ));
      setWebhookSummary(payload.summary);
      setWebhookTotal(payload.total);
      setWebhookStatusLine("");
    } catch (error) {
      setWebhookJobs([]);
      setWebhookSummary(null);
      setWebhookTotal(0);
      setWebhookStatusLine(`Webhook queue is unavailable: ${(error as Error).message}`);
    } finally {
      setWebhookBusy(false);
    }
  }

  async function handleRetryWebhookJob(jobId: string) {
    if (!credentials) return;
    setRetryingWebhookJobId(jobId);
    try {
      await retryWebhookJob(credentials, {
        tenantId: params.tenantId,
        projectId: params.projectId,
        jobId,
      });
      await loadWebhookJobs();
      showToast({
        title: "Webhook retry queued",
        description: `Retried webhook job ${jobId}.`,
        tone: "success",
      });
    } catch (error) {
      showToast({
        title: "Webhook retry failed",
        description: (error as Error).message,
        tone: "error",
      });
    } finally {
      setRetryingWebhookJobId(null);
    }
  }

  async function loadProject() {
    if (!credentials) return;
    setBusy(true);
    try {
      const payload = await getProject(credentials, params.tenantId, params.projectId);
      setProject(payload);
      setForm(buildProjectFormState(payload));
      setSecretRefs(payload.secret_refs ?? {});
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to load project: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) void loadProject();
  }, [ready, credentials, params.tenantId, params.projectId, allowProjectManagement, isPlatformSuperAdmin]);

  useEffect(() => {
    if (ready && credentials && project && activeTab === "settings" && allowProjectManagement) void loadOptions();
  }, [activeTab, allowProjectManagement, credentials, project, ready]);

  useEffect(() => {
    if (!ready || !credentials || activeTab !== "settings" || !allowProjectManagement || form.architecture_provider !== "confluence") {
      setConfluenceSpaces([]);
      setConfluencePages([]);
      setConfluenceCreateSpaceUrl(null);
      setConfluenceStatusLine("");
      return;
    }
    void loadConfluenceSpaces();
  }, [activeTab, allowProjectManagement, credentials, form.architecture_provider, params.tenantId, ready]);

  useEffect(() => {
    if (!ready || !credentials || activeTab !== "settings" || !allowProjectManagement || form.architecture_provider !== "confluence") {
      setConfluencePages([]);
      return;
    }
    if (!form.architecture_space_key.trim()) {
      setConfluencePages([]);
      return;
    }
    void loadConfluencePages(form.architecture_space_key);
  }, [
    activeTab,
    allowProjectManagement,
    credentials,
    form.architecture_provider,
    form.architecture_space_key,
    form.architecture_parent_page_id,
    params.tenantId,
    ready,
  ]);

  useEffect(() => {
    if (ready && credentials && project && isPlatformSuperAdmin && activeTab === "runs") void loadRuns();
  }, [activeTab, ready, credentials, isPlatformSuperAdmin, project, runFromDate, runIssueFilter, runPage, runPageSize, runPrFilter, runStatusFilter, runToDate]);

  useEffect(() => {
    if (ready && credentials && project && activeTab === "webhooks") void loadWebhookJobs();
  }, [
    activeTab,
    ready,
    credentials,
    project,
    params.tenantId,
    params.projectId,
    webhookPage,
    webhookPageSize,
    webhookQueryFilter,
    webhookStatusFilter,
    webhookTransportFilter,
  ]);

  useEffect(() => {
    if (activeTab !== "settings") {
      setActiveSettingsSection("general");
    }
  }, [activeTab]);

  async function toggleArchive() {
    if (!credentials || !project) return;
    if (!project.is_archived && archiveConfirmationName.trim() !== project.name.trim()) {
      return;
    }
    setBusy(true);
    try {
      const updated = await updateProjectArchiveState(credentials, params.tenantId, params.projectId, {
        is_archived: !project.is_archived,
      });
      if (updated.is_archived) {
        setArchiveConfirmationName("");
        router.push(getProjectArchiveRedirectRoute(principal, params.tenantId));
        return;
      }
      setProject(updated);
      setForm(buildProjectFormState(updated));
      setArchiveConfirmationName("");
      showToast({
        title: updated.is_archived ? "Project archived" : "Project unarchived",
        description: updated.name,
        tone: "success",
      });
    } catch (error) {
      showToast({
        title: "Project update failed",
        description: (error as Error).message,
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  }

  function buildNextPolicyOverrides(): Record<string, unknown> {
    if (!project) return {};
    const nextPolicyOverrides = { ...(project.policy_overrides ?? {}) };
    if (form.codex_model?.trim()) {
      nextPolicyOverrides.codex_model = form.codex_model.trim();
    } else {
      delete nextPolicyOverrides.codex_model;
    }
    if (form.codex_reasoning_effort) {
      nextPolicyOverrides.codex_reasoning_effort = form.codex_reasoning_effort;
    } else {
      delete nextPolicyOverrides.codex_reasoning_effort;
    }
    const allowJiraTransitions = booleanStateToOverride(form.allow_jira_transitions);
    const allowPrCreation = booleanStateToOverride(form.allow_pr_creation);
    const allowCodeReviews = booleanStateToOverride(form.allow_code_reviews);
    const allowPrRemediation = booleanStateToOverride(form.allow_pr_remediation);
    const allowManualPrFixRequests = booleanStateToOverride(form.allow_manual_pr_fix_requests);
    const allowLabelMutations = booleanStateToOverride(form.allow_label_mutations);
    const allowAutoMerge = booleanStateToOverride(form.allow_auto_merge);
    const stagingMergeCheckEnabled = booleanStateToOverride(form.staging_merge_check_enabled);
    const knowledgeBaseEnabled = booleanStateToOverride(form.knowledge_base_enabled);
    const maxDevTestReviewLoops = parsePositiveOverride(form.max_dev_test_review_loops);
    const maxPrAutoRemediationLoops = parsePositiveOverride(form.max_pr_auto_remediation_loops);
    const maxConcurrentRuns = parsePositiveOverride(form.max_concurrent_runs);
    if (allowJiraTransitions === undefined) {
      delete nextPolicyOverrides.allow_jira_transitions;
    } else {
      nextPolicyOverrides.allow_jira_transitions = allowJiraTransitions;
    }
    if (allowPrCreation === undefined) {
      delete nextPolicyOverrides.allow_pr_creation;
    } else {
      nextPolicyOverrides.allow_pr_creation = allowPrCreation;
    }
    if (allowCodeReviews === undefined) {
      delete nextPolicyOverrides.allow_code_reviews;
    } else {
      nextPolicyOverrides.allow_code_reviews = allowCodeReviews;
    }
    if (allowPrRemediation === undefined) {
      delete nextPolicyOverrides.allow_pr_remediation;
    } else {
      nextPolicyOverrides.allow_pr_remediation = allowPrRemediation;
    }
    if (allowManualPrFixRequests === undefined) {
      delete nextPolicyOverrides.allow_manual_pr_fix_requests;
    } else {
      nextPolicyOverrides.allow_manual_pr_fix_requests = allowManualPrFixRequests;
    }
    if (allowLabelMutations === undefined) {
      delete nextPolicyOverrides.allow_label_mutations;
    } else {
      nextPolicyOverrides.allow_label_mutations = allowLabelMutations;
    }
    if (allowAutoMerge === undefined) {
      delete nextPolicyOverrides.allow_auto_merge;
    } else {
      nextPolicyOverrides.allow_auto_merge = allowAutoMerge;
    }
    if (stagingMergeCheckEnabled === undefined) {
      delete nextPolicyOverrides.staging_admission_enabled;
    } else {
      nextPolicyOverrides.staging_admission_enabled = stagingMergeCheckEnabled;
    }
    if (form.staging_branch.trim()) {
      nextPolicyOverrides.staging_branch = form.staging_branch.trim();
    } else {
      delete nextPolicyOverrides.staging_branch;
    }
    if (form.require_agents_md === "required") {
      nextPolicyOverrides.require_agents_md = true;
    } else {
      delete nextPolicyOverrides.require_agents_md;
    }
    if (knowledgeBaseEnabled === undefined) {
      delete nextPolicyOverrides.knowledge_base_enabled;
    } else {
      nextPolicyOverrides.knowledge_base_enabled = knowledgeBaseEnabled;
    }
    if (form.knowledge_auto_answer_mode === "inherit") {
      delete nextPolicyOverrides.knowledge_auto_answer_mode;
    } else {
      nextPolicyOverrides.knowledge_auto_answer_mode = form.knowledge_auto_answer_mode;
    }
    if (maxDevTestReviewLoops === undefined) {
      delete nextPolicyOverrides.max_dev_test_review_loops;
    } else {
      nextPolicyOverrides.max_dev_test_review_loops = maxDevTestReviewLoops;
    }
    if (maxPrAutoRemediationLoops === undefined) {
      delete nextPolicyOverrides.max_pr_auto_remediation_loops;
    } else {
      nextPolicyOverrides.max_pr_auto_remediation_loops = maxPrAutoRemediationLoops;
    }
    if (maxConcurrentRuns === undefined) {
      delete nextPolicyOverrides.max_concurrent_runs;
    } else {
      nextPolicyOverrides.max_concurrent_runs = maxConcurrentRuns;
    }
    if (form.allowed_commands_mode === "inherit") {
      delete nextPolicyOverrides.allowed_commands;
    } else {
      nextPolicyOverrides.allowed_commands = form.allowed_commands_text
        .split("\n")
        .map((line) => line.trim())
        .filter((line, index, array) => line.length > 0 && array.indexOf(line) === index);
    }
    return nextPolicyOverrides;
  }

  async function saveDetails() {
    if (!credentials || !project) return;
    if (!form.name.trim() || !form.github_repository.trim() || !form.jira_project_key.trim()) {
      setStatusLine("Project name, repository, and Jira key are required.");
      return;
    }
    setBusy(true);
    try {
      const updated =
        activeSettingsSection === "general"
          ? await updateProjectConfiguration(credentials, params.tenantId, params.projectId, {
              name: form.name.trim(),
              github_repository: form.github_repository.trim(),
              jira_project_key: form.jira_project_key.trim().toUpperCase(),
              architecture_docs: buildArchitectureDocsPayload(form),
            })
          : await updateProjectPolicy(credentials, params.tenantId, params.projectId, {
              policy_overrides: buildNextPolicyOverrides(),
            });
      setProject(updated);
      setForm(buildProjectFormState(updated));
      setStatusLine("");
      showToast({
        title: activeSettingsSection === "general" ? "Project configuration saved" : "Project policy saved",
        description: updated.name,
        tone: "success",
      });
    } catch (error) {
      showToast({
        title: "Project update failed",
        description: (error as Error).message,
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  }

  async function resolveJiraRunBoard() {
    if (!credentials || !project) return;
    setBusy(true);
    try {
      const updated = await resolveProjectJiraRunBoard(credentials, params.tenantId, params.projectId);
      setProject(updated);
      setForm(buildProjectFormState(updated));
      showToast({
        title: "Jira board resolved",
        description: `Run board id ${String(updated.policy_overrides?.run_board_id ?? "")}`,
        tone: "success",
      });
    } catch (error) {
      showToast({
        title: "Jira board resolution failed",
        description: (error as Error).message,
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  }

  function projectSecretSource(ref: string): "project" | "tenant" | "platform" | "ref" {
    if (ref.startsWith(projectSecretPrefix)) {
      return "project";
    }
    if (ref.startsWith("tenant/")) {
      return "tenant";
    }
    if (ref.startsWith("platform/")) {
      return "platform";
    }
    return "ref";
  }

  async function refreshSecrets() {
    if (!credentials) return;
    setSecretsBusy(true);
    try {
      const payload = await getProject(credentials, params.tenantId, params.projectId);
      setProject(payload);
      setSecretRefs(payload.secret_refs ?? {});
      setSecretsStatusLine("");
    } catch (error) {
      setSecretsStatusLine(`Failed to load secrets: ${(error as Error).message}`);
    } finally {
      setSecretsBusy(false);
    }
  }

  async function saveSecretRefs(nextSecretRefs?: Record<string, string>): Promise<boolean> {
    if (!credentials || !project) return false;
    const refsToSave = nextSecretRefs ?? secretRefs;
    setSecretsBusy(true);
    try {
      const updated = await updateProjectSecretRefs(credentials, params.tenantId, params.projectId, {
        secret_refs: refsToSave,
      });
      setProject(updated);
      setSecretRefs(updated.secret_refs ?? {});
      setSecretsStatusLine("");
      return true;
    } catch (error) {
      showToast({
        title: "Project secrets save failed",
        description: (error as Error).message,
        tone: "error",
      });
      return false;
    } finally {
      setSecretsBusy(false);
    }
  }

  async function saveSecret() {
    const key = secretKey.trim();
    const value = secretValue.trim();
    if (!key || !value) {
      setSecretsStatusLine("Secret key and value are required.");
      return;
    }
    const nextRefs = { ...secretRefs, [key]: value };
    setSecretRefs(nextRefs);
    setSecretsStatusLine("");
    const saved = await saveSecretRefs(nextRefs);
    if (!saved) return;
    setSecretKey("");
    setSecretValue("");
    setEditingSecretKey(null);
    showToast({
      title: editingSecretKey ? "Secret updated" : "Secret saved",
      description: key,
      tone: "success",
    });
  }

  async function removeSecretRef(key: string) {
    const nextRefs = { ...secretRefs };
    delete nextRefs[key];
    setSecretRefs(nextRefs);
    setSecretsStatusLine("");
    const saved = await saveSecretRefs(nextRefs);
    if (saved && editingSecretKey === key) {
      setEditingSecretKey(null);
      setSecretKey("");
      setSecretValue("");
    }
    if (saved) {
      showToast({
        title: "Secret removed",
        description: key,
        tone: "success",
      });
    }
  }

  function startEditingSecret(key: string) {
    setEditingSecretKey(key);
    setSecretKey(key);
    setSecretValue("");
    setSecretsStatusLine(`Editing ${key}. Existing value is never shown.`);
  }

  function cancelEditingSecret() {
    setEditingSecretKey(null);
    setSecretKey("");
    setSecretValue("");
    setSecretsStatusLine("");
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2.5 min-w-0">
          <h1 className="truncate text-xl font-semibold">{project?.name ?? params.projectId}</h1>
          {project ? (
            <Badge variant={project.is_archived ? "outline" : "success"} className="shrink-0">
              {project.is_archived ? "Archived" : "Active"}
            </Badge>
          ) : null}
        </div>
      </div>

      <ProjectSectionTabs
        tenantId={params.tenantId}
        projectId={params.projectId}
        activeSection={activeTab}
        allowProjectManagement={allowProjectManagement}
        isPlatformSuperAdmin={isPlatformSuperAdmin}
      />

      {/* ── Overview tab ─────────────────────────────────────────────────── */}
      {activeTab === "overview" ? (
        <div className="space-y-4">
          {statusLine ? (
            <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p>
          ) : null}
          {project ? (
            <ProjectParentWorkBoard
              tenantId={params.tenantId}
              projectId={params.projectId}
              allowJiraReconciliation={allowProjectManagement}
            />
          ) : null}
          {!project ? (
            <p className="text-sm text-muted-foreground">Loading project details…</p>
          ) : null}
        </div>
      ) : null}

      {/* ── Settings tab ─────────────────────────────────────────────────── */}
      {activeTab === "settings" ? (
        <div className="space-y-4">
          {statusLine ? (
            <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p>
          ) : null}
          {project ? (
            <>
              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="flex flex-wrap items-center justify-between gap-3 p-6 pb-3">
                    <div>
                      <h2 className="text-base font-semibold">Settings</h2>
                    </div>
                    <div className="flex items-center gap-2">
                      <Button variant="ghost" size="sm" onClick={() => void loadOptions()} disabled={busy}>
                        <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
                        Refresh options
                      </Button>
                      <Button size="sm" onClick={() => void saveDetails()} disabled={busy}>
                        Save settings
                      </Button>
                    </div>
                </div>
                <div className="px-6 pb-6 space-y-4">
                  <div className="grid gap-2 md:grid-cols-5">
                    {SETTINGS_SECTIONS.map((section) => (
                      <button
                        key={section.id}
                        type="button"
                        onClick={() => setActiveSettingsSection(section.id)}
                        className={`rounded-xl border px-4 py-3 text-left transition-colors ${
                          activeSettingsSection === section.id
                            ? "border-primary bg-primary/5"
                            : "border-border bg-background hover:bg-muted/40"
                        }`}
                      >
                        <p className="text-sm font-medium text-foreground">{section.label}</p>
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              {activeSettingsSection === "general" ? (
                <div className="overflow-hidden rounded-2xl border bg-background">
                  <div className="p-6 pb-3">
                    <h2 className="text-base font-semibold">General</h2>
                  </div>
                  <div className="p-6 pt-0 grid gap-4 md:grid-cols-3">
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Project name
                      </label>
                      <Input
                        value={form.name}
                        onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
                        disabled={busy}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Repository
                      </label>
                      <Input
                        list="project-details-repo-options"
                        value={form.github_repository}
                        onChange={(e) => setForm((prev) => ({ ...prev, github_repository: e.target.value }))}
                        placeholder="Choose repository"
                        disabled={busy}
                      />
                      <datalist id="project-details-repo-options">
                        {repoOptions.map((repo) => <option key={repo} value={repo} />)}
                        {!repoOptions.includes(form.github_repository) && form.github_repository
                          ? <option value={form.github_repository} />
                          : null}
                      </datalist>
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Jira project
                      </label>
                      <Input
                        list="project-details-jira-options"
                        value={form.jira_project_key}
                        onChange={(e) =>
                          setForm((prev) => ({ ...prev, jira_project_key: e.target.value.toUpperCase() }))
                        }
                        placeholder="Choose Jira project"
                        disabled={busy}
                      />
                      <datalist id="project-details-jira-options">
                        {jiraOptions.map((key) => <option key={key} value={key} />)}
                        {!jiraOptions.includes(form.jira_project_key) && form.jira_project_key
                          ? <option value={form.jira_project_key} />
                          : null}
                      </datalist>
                      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                        <span>
                          Run board: {project.policy_overrides?.run_board_id ? String(project.policy_overrides.run_board_id) : "not resolved"}
                        </span>
                        <button
                          type="button"
                          className="font-medium text-foreground underline underline-offset-4 disabled:opacity-50"
                          onClick={() => void resolveJiraRunBoard()}
                          disabled={busy}
                        >
                          Resolve board
                        </button>
                      </div>
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Architecture docs provider
                      </label>
                      <select
                        className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                        value={form.architecture_provider}
                        onChange={(e) =>
                          setForm((prev) => ({
                            ...prev,
                            architecture_provider: e.target.value as ProjectFormState["architecture_provider"]
                          }))
                        }
                        disabled={busy}
                      >
                        <option value="">Disabled</option>
                        <option value="internal">Internal</option>
                        <option value="confluence">Confluence</option>
                      </select>
                    </div>
                    {form.architecture_provider === "confluence" ? (
                      <>
                        <div className="space-y-1.5">
                          <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                            Confluence space
                          </label>
                          <select
                            className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                            value={form.architecture_space_key}
                            onChange={(e) =>
                              setForm((prev) => ({
                                ...prev,
                                architecture_space_key: e.target.value,
                                architecture_parent_page_id: "",
                              }))
                            }
                            disabled={busy}
                          >
                            <option value="">
                              {confluenceSpacesLoading
                                ? "Loading Confluence spaces..."
                                : confluenceSpaces.length === 0
                                  ? "No Confluence spaces available"
                                  : "Select Confluence space"}
                            </option>
                            {confluenceSpaces.map((space) => (
                              <option key={space.space_id} value={space.key}>
                                {space.name} ({space.key})
                              </option>
                            ))}
                            {!confluenceSpaces.some((space) => space.key === form.architecture_space_key) && form.architecture_space_key ? (
                              <option value={form.architecture_space_key}>
                                {form.architecture_space_key} (configured)
                              </option>
                            ) : null}
                          </select>
                          {confluenceSpaces.length === 0 && confluenceCreateSpaceUrl ? (
                            <div className="text-xs text-muted-foreground">
                              <a
                                href={confluenceCreateSpaceUrl}
                                target="_blank"
                                rel="noreferrer"
                                className="font-medium text-foreground underline underline-offset-4"
                              >
                                Create a Confluence space
                              </a>
                            </div>
                          ) : null}
                        </div>
                        <div className="space-y-1.5">
                          <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                            Confluence parent page
                          </label>
                          <select
                            className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                            value={form.architecture_parent_page_id}
                            onChange={(e) =>
                              setForm((prev) => ({ ...prev, architecture_parent_page_id: e.target.value }))
                            }
                            disabled={busy || !form.architecture_space_key.trim()}
                          >
                            <option value="">
                              {!form.architecture_space_key.trim()
                                ? "Top-level in selected space"
                                : confluencePagesLoading
                                  ? "Loading space pages..."
                                  : "Top-level in selected space"}
                            </option>
                            {confluencePages.map((page) => (
                              <option key={page.page_id} value={page.page_id}>
                                {page.title}
                              </option>
                            ))}
                            {!confluencePages.some((page) => page.page_id === form.architecture_parent_page_id) &&
                            form.architecture_parent_page_id ? (
                              <option value={form.architecture_parent_page_id}>
                                Current parent page ({form.architecture_parent_page_id})
                              </option>
                            ) : null}
                          </select>
                        </div>
                        {confluenceStatusLine ? (
                          <div className="md:col-span-3 text-sm text-muted-foreground">{confluenceStatusLine}</div>
                        ) : null}
                      </>
                    ) : null}
                    <div className="md:col-span-3 rounded-xl border bg-muted/30 p-4">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div className="space-y-1">
                          <p className="text-sm font-medium text-foreground">Architecture documents</p>
                          <p className="text-xs text-muted-foreground">
                            Jira tickets should reference architecture pages instead of storing architecture inline.
                          </p>
                        </div>
                        <Button asChild variant="outline" size="sm">
                          <Link href={`/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(params.projectId)}/architecture`}>
                            Open architecture docs
                            <ExternalLink className="ml-1.5 h-3.5 w-3.5" />
                          </Link>
                        </Button>
                      </div>
                    </div>
                  </div>
                </div>
              ) : null}

              {activeSettingsSection === "ai" ? (
                <div className="overflow-hidden rounded-2xl border bg-background">
                  <div className="p-6 pb-3">
                    <h2 className="text-base font-semibold">AI</h2>
                  </div>
                  <div className="p-6 pt-0 grid gap-4 md:grid-cols-2">
                    <div className="space-y-1.5 md:col-span-2">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Execution model override
                      </label>
                      <CodexModelSelect
                        value={form.codex_model}
                        models={codexModels}
                        inheritLabel="Inherit tenant model"
                        effectiveLabel={`Effective model: ${project.effective_policy.codex_model ?? (globalCodexModel || "global default")}`}
                        helperText={globalCodexModel ? `Global engineering runtime default: ${globalCodexModel}` : undefined}
                        disabled={busy}
                        onChange={(next) => setForm((prev) => ({ ...prev, codex_model: next }))}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Reasoning mode
                      </label>
                      <select
                        className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                        value={form.codex_reasoning_effort ?? ""}
                        disabled={busy || reasoningEfforts.length === 0}
                        onChange={(e) =>
                          setForm((prev) => ({
                            ...prev,
                            codex_reasoning_effort: (e.target.value || null) as ProjectFormState["codex_reasoning_effort"],
                          }))
                        }
                      >
                        <option value="">
                          {reasoningEfforts.length > 0
                            ? "Inherit tenant reasoning mode"
                            : "Not supported by the current engineering runtime"}
                        </option>
                        {reasoningEfforts.map((option) => (
                          <option key={option.id} value={option.id}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                      <p className="text-xs text-muted-foreground">
                        Effective: {project.effective_policy.codex_reasoning_effort ?? (globalCodexReasoningEffort || "medium")}
                      </p>
                    </div>
                  </div>
                </div>
              ) : null}

              {activeSettingsSection === "automation" ? (
                <div className="overflow-hidden rounded-2xl border bg-background">
                  <div className="p-6 pb-3">
                    <h2 className="text-base font-semibold">Automation</h2>
                  </div>
                  <div className="p-6 pt-0 grid gap-4 md:grid-cols-2">
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Jira transitions
                      </label>
                      <OverrideSegmentedControl
                        value={form.allow_jira_transitions}
                        options={BOOLEAN_OVERRIDE_OPTIONS}
                        onChange={(next) => setForm((prev) => ({ ...prev, allow_jira_transitions: next }))}
                        disabled={busy}
                      />
                      <p className="text-xs text-muted-foreground">Effective: {formatBoolean(project.effective_policy.allow_jira_transitions)}</p>
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        PR creation
                      </label>
                      <OverrideSegmentedControl
                        value={form.allow_pr_creation}
                        options={BOOLEAN_OVERRIDE_OPTIONS}
                        onChange={(next) => setForm((prev) => ({ ...prev, allow_pr_creation: next }))}
                        disabled={busy}
                      />
                      <p className="text-xs text-muted-foreground">Effective: {formatBoolean(project.effective_policy.allow_pr_creation)}</p>
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Code review
                      </label>
                      <OverrideSegmentedControl
                        value={form.allow_code_reviews}
                        options={BOOLEAN_OVERRIDE_OPTIONS}
                        onChange={(next) => setForm((prev) => ({ ...prev, allow_code_reviews: next }))}
                        disabled={busy}
                      />
                      <p className="text-xs text-muted-foreground">Effective: {formatBoolean(project.effective_policy.allow_code_reviews)}</p>
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        PR remediation
                      </label>
                      <OverrideSegmentedControl
                        value={form.allow_pr_remediation}
                        options={BOOLEAN_OVERRIDE_OPTIONS}
                        onChange={(next) => setForm((prev) => ({ ...prev, allow_pr_remediation: next }))}
                        disabled={busy}
                      />
                      <p className="text-xs text-muted-foreground">Effective: {formatBoolean(project.effective_policy.allow_pr_remediation)}</p>
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Manual PR fix requests
                      </label>
                      <OverrideSegmentedControl
                        value={form.allow_manual_pr_fix_requests}
                        options={BOOLEAN_OVERRIDE_OPTIONS}
                        onChange={(next) => setForm((prev) => ({ ...prev, allow_manual_pr_fix_requests: next }))}
                        disabled={busy}
                      />
                      <p className="text-xs text-muted-foreground">
                        Effective: {formatBoolean(project.effective_policy.allow_manual_pr_fix_requests)}
                      </p>
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Label mutations
                      </label>
                      <OverrideSegmentedControl
                        value={form.allow_label_mutations}
                        options={BOOLEAN_OVERRIDE_OPTIONS}
                        onChange={(next) => setForm((prev) => ({ ...prev, allow_label_mutations: next }))}
                        disabled={busy}
                      />
                      <p className="text-xs text-muted-foreground">Effective: {formatBoolean(project.effective_policy.allow_label_mutations)}</p>
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Auto merge
                      </label>
                      <OverrideSegmentedControl
                        value={form.allow_auto_merge}
                        options={BOOLEAN_OVERRIDE_OPTIONS}
                        onChange={(next) => setForm((prev) => ({ ...prev, allow_auto_merge: next }))}
                        disabled={busy}
                      />
                      <p className="text-xs text-muted-foreground">Effective: {formatBoolean(project.effective_policy.allow_auto_merge)}</p>
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Staging merge check
                      </label>
                      <OverrideSegmentedControl
                        value={form.staging_merge_check_enabled}
                        options={BOOLEAN_OVERRIDE_OPTIONS}
                        onChange={(next) => setForm((prev) => ({ ...prev, staging_merge_check_enabled: next }))}
                        disabled={busy}
                      />
                      <p className="text-xs text-muted-foreground">
                        Publish the GitHub check that keeps staging-targeted PRs fresh against the current staging head.
                      </p>
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Staging branch
                      </label>
                      <Input
                        value={form.staging_branch}
                        onChange={(e) => setForm((prev) => ({ ...prev, staging_branch: e.target.value }))}
                        placeholder="staging"
                        disabled={busy}
                      />
                      <p className="text-xs text-muted-foreground">
                        Branch name to validate before the GitHub merge button is used.
                      </p>
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Max dev/test/review loops
                      </label>
                      <Input
                        type="number"
                        value={form.max_dev_test_review_loops}
                        onChange={(e) => setForm((prev) => ({ ...prev, max_dev_test_review_loops: e.target.value }))}
                        placeholder={`Inherit (${project.effective_policy.max_dev_test_review_loops})`}
                        disabled={busy}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Max PR remediation loops
                      </label>
                      <Input
                        type="number"
                        value={form.max_pr_auto_remediation_loops}
                        onChange={(e) => setForm((prev) => ({ ...prev, max_pr_auto_remediation_loops: e.target.value }))}
                        placeholder={`Inherit (${project.effective_policy.max_pr_auto_remediation_loops})`}
                        disabled={busy}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Max concurrent runs
                      </label>
                      <Input
                        type="number"
                        value={form.max_concurrent_runs}
                        onChange={(e) => setForm((prev) => ({ ...prev, max_concurrent_runs: e.target.value }))}
                        placeholder={`Inherit (${project.effective_policy.max_concurrent_runs})`}
                        disabled={busy}
                      />
                    </div>
                    <div className="space-y-1.5 md:col-span-2">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Allowed commands override
                      </label>
                      <select
                        className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                        value={form.allowed_commands_mode}
                        onChange={(e) => setForm((prev) => ({ ...prev, allowed_commands_mode: e.target.value as AllowedCommandsMode }))}
                        disabled={busy}
                      >
                        <option value="inherit">Inherit tenant commands</option>
                        <option value="custom">Set project command allowlist</option>
                      </select>
                      {form.allowed_commands_mode === "custom" ? (
                        <Textarea
                          value={form.allowed_commands_text}
                          onChange={(e) => setForm((prev) => ({ ...prev, allowed_commands_text: e.target.value }))}
                          placeholder="git status"
                          className="min-h-[110px]"
                          disabled={busy}
                        />
                      ) : null}
                      <p className="text-xs text-muted-foreground">
                        Effective commands: {(project.effective_policy.allowed_commands ?? []).length > 0 ? (project.effective_policy.allowed_commands ?? []).join(", ") : "none"}
                      </p>
                    </div>
                  </div>
                </div>
              ) : null}

              {activeSettingsSection === "knowledge" ? (
                <div className="overflow-hidden rounded-2xl border bg-background">
                  <div className="p-6 pb-3">
                    <h2 className="text-base font-semibold">Knowledge</h2>
                  </div>
                  <div className="p-6 pt-0 grid gap-4 md:grid-cols-2">
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Knowledge base
                      </label>
                      <OverrideSegmentedControl
                        value={form.knowledge_base_enabled}
                        options={BOOLEAN_OVERRIDE_OPTIONS}
                        onChange={(next) => setForm((prev) => ({ ...prev, knowledge_base_enabled: next }))}
                        disabled={busy}
                      />
                      <p className="text-xs text-muted-foreground">Effective: {formatBoolean(project.effective_policy.knowledge_base_enabled)}</p>
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Knowledge answer mode
                      </label>
                      <select
                        className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                        value={form.knowledge_auto_answer_mode}
                        onChange={(e) => setForm((prev) => ({ ...prev, knowledge_auto_answer_mode: e.target.value as KnowledgeModeValue }))}
                        disabled={busy}
                      >
                        <option value="inherit">Inherit tenant setting</option>
                        <option value="safe">Safe</option>
                        <option value="balanced">Balanced</option>
                        <option value="aggressive">Aggressive</option>
                      </select>
                      <p className="text-xs text-muted-foreground">Effective: {project.effective_policy.knowledge_auto_answer_mode}</p>
                    </div>
                  </div>
                </div>
              ) : null}

              {activeSettingsSection === "governance" ? (
                <div className="overflow-hidden rounded-2xl border bg-background">
                  <div className="p-6 pb-3">
                    <h2 className="text-base font-semibold">Governance</h2>
                  </div>
                  <div className="p-6 pt-0 grid gap-4 md:grid-cols-2">
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Require AGENTS.md
                      </label>
                      <select
                        className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                        value={form.require_agents_md}
                        onChange={(e) => setForm((prev) => ({ ...prev, require_agents_md: e.target.value as RequireAgentsValue }))}
                        disabled={busy}
                      >
                        <option value="inherit">Inherit tenant setting</option>
                        <option value="required">Require AGENTS.md</option>
                      </select>
                      <p className="text-xs text-muted-foreground">Effective: {project.effective_policy.require_agents_md ? "Required" : "Not required"}</p>
                    </div>
                  </div>
                </div>
              ) : null}
            </>
          ) : (
            <p className="text-sm text-muted-foreground">Loading project details…</p>
          )}
        </div>
      ) : null}

      {/* ── Runs tab ─────────────────────────────────────────────────────── */}
      {activeTab === "runs" ? (
        <div className="overflow-hidden rounded-2xl border bg-background">
          <div className="flex items-center justify-between gap-2 border-b px-5 py-3">
            <h2 className="text-sm font-semibold">Project Runs</h2>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void loadRuns()}
              disabled={runsBusy}
            >
              <RefreshCw className={`h-3.5 w-3.5 ${runsBusy ? "animate-spin" : ""}`} />
            </Button>
          </div>

          <div className="overflow-x-auto border-b px-5 py-3">
            <div className="flex min-w-max flex-nowrap items-center gap-2 md:min-w-0 md:flex-wrap">
              <SlidersHorizontal className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
              <Input
                className="h-8 w-40 text-sm"
                value={runIssueFilter}
                onChange={(e) => {
                  setRunIssueFilter(e.target.value);
                  setRunPage(1);
                }}
                placeholder="Issue / summary"
                disabled={runsBusy}
              />
              <select
                className="h-8 w-36 rounded-md border border-input bg-background px-2 text-sm"
                value={runStatusFilter}
                onChange={(e) => {
                  setRunStatusFilter(e.target.value as RunStatus | "all");
                  setRunPage(1);
                }}
                disabled={runsBusy}
              >
                <option value="all">Status: Any</option>
                {RUN_STATUSES.map((status) => (
                  <option key={status} value={status}>
                    {status}
                  </option>
                ))}
              </select>
              <select
                className="h-8 rounded-md border border-input bg-background px-2 text-sm"
                value={runPrFilter}
                onChange={(e) => {
                  setRunPrFilter(e.target.value as "any" | "none" | "has_value");
                  setRunPage(1);
                }}
                disabled={runsBusy}
              >
                <option value="any">PR: Any</option>
                <option value="none">PR: None</option>
                <option value="has_value">PR: Has value</option>
              </select>
              <Input
                type="date"
                className="h-8 w-36 text-sm"
                value={runFromDate}
                onChange={(e) => {
                  setRunFromDate(e.target.value);
                  setRunPage(1);
                }}
                disabled={runsBusy}
              />
              <Input
                type="date"
                className="h-8 w-36 text-sm"
                value={runToDate}
                onChange={(e) => {
                  setRunToDate(e.target.value);
                  setRunPage(1);
                }}
                disabled={runsBusy}
              />
              <select
                className="h-8 rounded-md border border-input bg-background px-2 text-sm"
                value={String(runPageSize)}
                onChange={(e) => {
                  setRunPageSize(Number(e.target.value) as 25 | 50 | 100);
                  setRunPage(1);
                }}
                disabled={runsBusy}
              >
                <option value="25">25 / page</option>
                <option value="50">50 / page</option>
                <option value="100">100 / page</option>
              </select>
              <Button
                size="sm"
                variant="secondary"
                className="h-8"
                onClick={() => void loadRuns()}
                disabled={runsBusy}
              >
                Apply
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="h-8"
                onClick={() => {
                  setRunIssueFilter("");
                  setRunStatusFilter("all");
                  setRunPrFilter("any");
                  setRunFromDate("");
                  setRunToDate("");
                  setRunPage(1);
                }}
                disabled={runsBusy}
              >
                Clear
              </Button>
            </div>
          </div>

          {runsStatusLine ? (
            <div className="border-b px-5 py-3 text-sm text-muted-foreground">
              {runsStatusLine}
            </div>
          ) : null}

          {runs.length === 0 ? (
            <div className="px-5 py-12 text-center text-sm text-muted-foreground">
              No runs found for this project.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-5">Run</TableHead>
                  <TableHead>Issue</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>PR</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((run) => (
                  <TableRow
                    key={run.run_id}
                    className={`border-l-2 ${STATUS_BORDER[run.status] ?? "border-l-transparent"}`}
                  >
                    <TableCell className="pl-5 font-medium">
                      <Link
                        className="text-primary hover:underline"
                        href={buildRunDetailPath({
                          tenantId: params.tenantId,
                          projectId: params.projectId,
                          runId: run.run_id,
                        })}
                      >
                        {run.issue_summary?.trim() || run.issue_key || run.run_id}
                      </Link>
                      <p className="text-xs text-muted-foreground font-mono">{run.run_id}</p>
                    </TableCell>
                    <TableCell>
                      {run.issue_url ? (
                        <Link
                          className="text-primary hover:underline text-sm"
                          href={run.issue_url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          {run.issue_key}
                        </Link>
                      ) : (
                        <span className="text-muted-foreground">{run.issue_key ?? "—"}</span>
                      )}
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={run.status} />
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground whitespace-nowrap">
                      {formatTimestamp(run.created_at)}
                    </TableCell>
                    <TableCell>
                      {run.pr_url ? (
                        <Link
                          className="inline-flex items-center gap-1 text-sm text-primary hover:underline"
                          href={run.pr_url}
                          target="_blank"
                        >
                          PR <ExternalLink className="h-3 w-3" />
                        </Link>
                      ) : (
                        <span className="text-xs text-muted-foreground">—</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}

          <div className="flex items-center justify-between border-t px-5 py-3">
            <span className="text-xs text-muted-foreground">
              {runs.length} run{runs.length !== 1 ? "s" : ""}
            </span>
            <div className="flex items-center gap-1">
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                onClick={() => setRunPage((prev) => Math.max(1, prev - 1))}
                disabled={runsBusy || runPage <= 1}
              >
                Prev
              </Button>
              <span className="px-2 text-xs text-muted-foreground">Page {runPage}</span>
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                onClick={() => setRunPage((prev) => prev + 1)}
                disabled={runsBusy || runs.length < runPageSize}
              >
                Next
              </Button>
            </div>
          </div>
        </div>
      ) : null}

      {/* ── Webhooks tab ───────────────────────────────────────────────────── */}
      {activeTab === "webhooks" ? (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {[
              { label: "Pending", value: webhookSummary?.pending_count ?? 0 },
              { label: "Processing", value: webhookSummary?.processing_count ?? 0 },
              { label: "Failed", value: webhookSummary?.failed_count ?? 0 },
              { label: "Done", value: webhookSummary?.done_count ?? 0 },
            ].map((kpi) => (
              <div key={kpi.label} className="rounded-xl border bg-background px-4 py-3">
                <p className="text-xs text-muted-foreground">{kpi.label}</p>
                <p className="mt-0.5 text-xl font-semibold">{kpi.value}</p>
              </div>
            ))}
          </div>

          {webhookStatusLine ? (
            <div className="rounded-xl border px-4 py-3 text-sm text-muted-foreground">{webhookStatusLine}</div>
          ) : null}

          <div className="overflow-hidden rounded-2xl border bg-background">
            <div className="flex items-center justify-between border-b px-5 py-3">
              <h2 className="text-sm font-semibold">Webhook Queue</h2>
              <Button variant="ghost" size="sm" onClick={() => void loadWebhookJobs()} disabled={webhookBusy}>
                <RefreshCw className={`h-3.5 w-3.5 ${webhookBusy ? "animate-spin" : ""}`} />
              </Button>
            </div>

            <div className="flex flex-col gap-3 border-b px-5 py-3 sm:flex-row sm:items-end">
              <div className="min-w-0 flex-1">
                <label className="mb-1 block text-xs font-medium text-muted-foreground" htmlFor="webhook-filter-query">
                  Issue / run / error
                </label>
                <Input
                  id="webhook-filter-query"
                  value={webhookQueryFilter}
                  onChange={(event) => {
                    setWebhookQueryFilter(event.target.value);
                    setWebhookPage(1);
                  }}
                  placeholder="Filter by issue key, run id, or error"
                  className="h-9"
                />
              </div>
              <div className="w-full sm:w-40">
                <label className="mb-1 block text-xs font-medium text-muted-foreground" htmlFor="webhook-filter-status">
                  Status
                </label>
                <select
                  id="webhook-filter-status"
                  value={webhookStatusFilter}
                  onChange={(event) => {
                    setWebhookStatusFilter(event.target.value);
                    setWebhookPage(1);
                  }}
                  className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                >
                  <option value="all">All statuses</option>
                  {webhookStatusOptions.map((status) => (
                    <option key={status} value={status}>
                      {status}
                    </option>
                  ))}
                </select>
              </div>
              <div className="w-full sm:w-44">
                <label className="mb-1 block text-xs font-medium text-muted-foreground" htmlFor="webhook-filter-transport">
                  Transport
                </label>
                <select
                  id="webhook-filter-transport"
                  value={webhookTransportFilter}
                  onChange={(event) => {
                    setWebhookTransportFilter(event.target.value);
                    setWebhookPage(1);
                  }}
                  className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                >
                  <option value="all">All transports</option>
                  {webhookTransportOptions.map((transport) => (
                    <option key={transport} value={transport}>
                      {transport}
                    </option>
                  ))}
                </select>
              </div>
              <div className="w-full sm:w-28">
                <label className="mb-1 block text-xs font-medium text-muted-foreground" htmlFor="webhook-page-size">
                  Page size
                </label>
                <select
                  id="webhook-page-size"
                  value={String(webhookPageSize)}
                  onChange={(event) => {
                    const nextValue = Number(event.target.value);
                    if (nextValue === 25 || nextValue === 50 || nextValue === 100) {
                      setWebhookPageSize(nextValue);
                      setWebhookPage(1);
                    }
                  }}
                  className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                >
                  <option value="25">25</option>
                  <option value="50">50</option>
                  <option value="100">100</option>
                </select>
              </div>
              <div className="flex items-center justify-between gap-3 sm:pb-1">
                <span className="text-xs text-muted-foreground">
                  {webhookJobs.length} of {webhookTotal}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 px-2 text-xs"
                  onClick={() => {
                    setWebhookQueryFilter("");
                    setWebhookStatusFilter("all");
                    setWebhookTransportFilter("all");
                    setWebhookPage(1);
                  }}
                  disabled={
                    webhookQueryFilter.length === 0 &&
                    webhookStatusFilter === "all" &&
                    webhookTransportFilter === "all"
                  }
                >
                  Clear
                </Button>
              </div>
            </div>

            {webhookJobs.length === 0 ? (
              <div className="px-5 py-12 text-center text-sm text-muted-foreground">
                {webhookTotal === 0
                  ? "No webhook jobs found for this project."
                  : "No webhook jobs match the current page."}
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="pl-5">Status</TableHead>
                    <TableHead>Transport</TableHead>
                    <TableHead>Subject</TableHead>
                    <TableHead>Arrived</TableHead>
                    <TableHead>Run</TableHead>
                    <TableHead>Attempts</TableHead>
                    <TableHead>Last Error</TableHead>
                    <TableHead className="pr-5 text-right">Action</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {webhookJobs.map((job) => (
                    <TableRow
                      key={job.job_id}
                      data-testid={`webhook-job-row-${job.job_id}`}
                      tabIndex={0}
                      aria-selected={selectedWebhookJobId === job.job_id}
                      className="cursor-pointer"
                      onClick={() => setSelectedWebhookJobId(job.job_id)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          setSelectedWebhookJobId(job.job_id);
                        }
                      }}
                    >
                      <TableCell className="pl-5">
                        <StatusBadge status={job.status} />
                      </TableCell>
                      <TableCell className="font-mono text-xs">{job.transport}</TableCell>
                      <TableCell className="max-w-[280px] truncate font-mono text-xs" title={job.subject_key}>
                        {job.subject_key}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {formatTimestamp(job.created_at, "—")}
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {job.related_run_id ? (
                          <Link
                            href={buildRunDetailPath({
                              tenantId: params.tenantId,
                              projectId: params.projectId,
                              runId: job.related_run_id,
                            })}
                            onClick={(event) => event.stopPropagation()}
                            className="text-primary underline-offset-4 hover:underline"
                          >
                            {job.related_run_id}
                          </Link>
                        ) : (
                          "—"
                        )}
                      </TableCell>
                      <TableCell className="text-xs">{job.attempt_count}</TableCell>
                      <TableCell className="max-w-[300px] truncate text-xs text-muted-foreground" title={job.last_error ?? ""}>
                        {job.last_error ?? "—"}
                      </TableCell>
                      <TableCell className="pr-5 text-right">
                        {job.status === "failed" ? (
                          <Button
                            variant="outline"
                            size="sm"
                            className="h-7 text-xs"
                            onClick={(event) => {
                              event.stopPropagation();
                              void handleRetryWebhookJob(job.job_id);
                            }}
                            disabled={webhookBusy || retryingWebhookJobId === job.job_id}
                          >
                            {retryingWebhookJobId === job.job_id ? "Retrying..." : "Retry"}
                          </Button>
                        ) : (
                          <span className="text-xs text-muted-foreground">—</span>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}

            <div className="flex items-center justify-between border-t px-5 py-3">
              <span className="text-xs text-muted-foreground">
                Page {webhookPage}
              </span>
              <div className="flex items-center gap-1">
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 text-xs"
                  onClick={() => setWebhookPage((prev) => Math.max(1, prev - 1))}
                  disabled={webhookBusy || webhookPage <= 1}
                >
                  Prev
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 text-xs"
                  onClick={() => setWebhookPage((prev) => prev + 1)}
                  disabled={webhookBusy || webhookPage * webhookPageSize >= webhookTotal}
                >
                  Next
                </Button>
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {/* ── Notifications tab ────────────────────────────────────────────── */}
      {activeTab === "notifications" ? (
        <ProjectNotificationsContent
          tenantId={params.tenantId}
          projectId={params.projectId}
          credentials={credentials}
        />
      ) : null}

      {activeTab === "automations" ? (
        <ProjectAutomationsContent
          tenantId={params.tenantId}
          projectId={params.projectId}
          credentials={credentials}
        />
      ) : null}

      {/* ── Danger tab ───────────────────────────────────────────────────── */}
      {activeTab === "danger" && project ? (
        <div className="space-y-6">
          <div className="overflow-hidden rounded-2xl border bg-background">
            <div className="px-6 pt-6">
              <h2 className="text-base font-semibold">Danger zone</h2>
            </div>
            <div className="divide-y">
              <div className="flex flex-wrap items-center justify-between gap-4 px-6 py-5">
                <div className="space-y-0.5">
                  <p className="text-sm font-medium">
                    {project.is_archived ? "Unarchive this project" : "Archive this project"}
                  </p>
                  <p className="text-sm text-muted-foreground">
                    {project.is_archived
                      ? "Restore this project to the active workspace."
                      : "Archiving removes the project from the active list immediately. It can be restored later."}
                  </p>
                </div>
                <Button
                  variant="outline"
                  className={
                    project.is_archived
                      ? undefined
                      : "border-red-300 text-red-600 hover:bg-red-50 hover:text-red-700"
                  }
                  onClick={() => {
                    if (!project.is_archived) {
                      setShowArchiveConfirm(true);
                      setArchiveConfirmationName("");
                    } else {
                      void toggleArchive();
                    }
                  }}
                  disabled={busy}
                >
                  <Archive className="mr-1.5 h-3.5 w-3.5" />
                  {project.is_archived ? "Unarchive project" : "Archive project…"}
                </Button>
              </div>
            </div>
          </div>

          {showArchiveConfirm ? (
            <div className="fixed inset-0 z-50 flex items-center justify-center">
              <div
                className="fixed inset-0 bg-black/50"
                onClick={() => setShowArchiveConfirm(false)}
              />
              <div className="relative mx-4 w-full max-w-md rounded-2xl border bg-background p-6 shadow-lg">
                <h3 className="text-lg font-semibold">Archive project</h3>
                <p className="mt-2 text-sm text-muted-foreground">
                  This will remove <span className="font-medium text-foreground">{project.name}</span> from the
                  active workspace immediately.
                </p>
                <div className="mt-4 space-y-2">
                  <p className="text-sm">
                    To confirm, type <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-sm">{project.name}</span> below.
                  </p>
                  <Input
                    value={archiveConfirmationName}
                    onChange={(event) => setArchiveConfirmationName(event.target.value)}
                    placeholder={project.name}
                    autoFocus
                    disabled={busy}
                  />
                </div>
                <div className="mt-6 flex justify-end gap-3">
                  <Button
                    variant="outline"
                    onClick={() => {
                      setShowArchiveConfirm(false);
                      setArchiveConfirmationName("");
                    }}
                    disabled={busy}
                  >
                    Cancel
                  </Button>
                  <Button
                    className="border-red-300 bg-red-600 text-white hover:bg-red-700 hover:text-white"
                    onClick={() => {
                      void toggleArchive().then(() => setShowArchiveConfirm(false));
                    }}
                    disabled={
                      busy ||
                      archiveConfirmationName.trim() !== project.name.trim()
                    }
                  >
                    {busy ? "Archiving…" : "Archive project"}
                  </Button>
                </div>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {/* ── Secrets tab ──────────────────────────────────────────────────── */}
      {activeTab === "secrets" ? (
        <div className="space-y-4">
          <div className="flex items-center justify-end">
            <Button variant="outline" size="sm" onClick={() => void refreshSecrets()} disabled={secretsBusy}>
              <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${secretsBusy ? "animate-spin" : ""}`} />
              Refresh
            </Button>
          </div>

          {secretsStatusLine ? (
            <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{secretsStatusLine}</p>
          ) : null}

          <div className="overflow-hidden rounded-2xl border bg-background">
            <div className="p-6 pb-3">
              <h2 className="text-base font-semibold">{editingSecretKey ? "Edit Secret" : "Add Secret"}</h2>
              {editingSecretKey ? (
                <p className="mt-1 text-sm text-warning">
                  Editing <code className="rounded bg-muted px-1 font-mono text-xs">{editingSecretKey}</code>.
                </p>
              ) : null}
            </div>
            <div className="px-6 pb-6 space-y-4">
              <div className="grid gap-4 md:grid-cols-2">
                <div className="space-y-1.5">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Secret key
                  </label>
                  <Input
                    value={secretKey}
                    onChange={(event) => setSecretKey(event.target.value)}
                    placeholder="e.g. OPENAI_KEY"
                    className="font-mono"
                    disabled={secretsBusy || Boolean(editingSecretKey)}
                  />
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    {editingSecretKey ? "New value" : "Secret value"}
                  </label>
                  <Textarea
                    value={secretValue}
                    onChange={(event) => setSecretValue(event.target.value)}
                    placeholder="Paste secret value here"
                    className="min-h-[72px] font-mono text-xs"
                    disabled={secretsBusy}
                  />
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Button size="sm" onClick={() => void saveSecret()} disabled={secretsBusy}>
                  <Save className="mr-1.5 h-3.5 w-3.5" />
                  {editingSecretKey ? "Update" : "Save secret"}
                </Button>
                {editingSecretKey ? (
                  <Button size="sm" variant="ghost" onClick={cancelEditingSecret} disabled={secretsBusy}>
                    <X className="mr-1.5 h-3.5 w-3.5" />
                    Cancel edit
                  </Button>
                ) : null}
              </div>
            </div>
          </div>

          <div className="overflow-hidden rounded-2xl border bg-background">
            <div className="flex items-center justify-between gap-2 p-6 pb-3">
                <h2 className="text-base font-semibold">Stored Secrets</h2>
                {Object.keys(secretRefs).length > 0 ? (
                  <Badge variant="outline" className="text-xs">
                    {Object.keys(secretRefs).length} secret{Object.keys(secretRefs).length !== 1 ? "s" : ""}
                  </Badge>
                ) : null}
            </div>
            <div>
            {Object.keys(secretRefs).length === 0 ? (
              <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
                <KeyRound className="h-6 w-6 text-muted-foreground" />
                <p className="text-sm font-medium">No project secrets stored yet</p>
                <p className="text-xs text-muted-foreground">Add a secret key and value above to get started.</p>
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow className="bg-muted/40">
                    <TableHead className="pl-6">Key</TableHead>
                    <TableHead>Source</TableHead>
                    <TableHead>Value</TableHead>
                    <TableHead className="text-right pr-6">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {Object.entries(secretRefs).map(([key, ref]) => (
                    <TableRow key={key} className={editingSecretKey === key ? "bg-warning/5" : undefined}>
                      <TableCell className="pl-6">
                        <div className="font-mono text-xs font-medium">{key}</div>
                      </TableCell>
                      <TableCell>
                        <Badge variant={projectSecretSource(ref) === "project" ? "default" : "outline"} className="text-[11px]">
                          {projectSecretSource(ref)}
                        </Badge>
                      </TableCell>
                      <TableCell className="font-mono text-xs text-muted-foreground">********</TableCell>
                      <TableCell className="text-right pr-6">
                        <div className="flex items-center justify-end gap-1">
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-7 px-2 text-xs"
                            onClick={() => startEditingSecret(key)}
                            disabled={secretsBusy}
                          >
                            <Pencil className="mr-1 h-3 w-3" />
                            Edit
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-7 px-2 text-xs text-destructive hover:text-destructive"
                            onClick={() => void removeSecretRef(key)}
                            disabled={secretsBusy}
                          >
                            <Trash2 className="mr-1 h-3 w-3" />
                            Delete
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            </div>
          </div>
        </div>
      ) : null}

      {selectedWebhookJob ? (
        <div className="fixed inset-0 z-50" role="dialog" aria-modal="true" aria-label="Webhook job details">
          <button
            type="button"
            aria-label="Close webhook job details"
            className="absolute inset-0 bg-black/40"
            onClick={() => setSelectedWebhookJobId(null)}
          />
          <aside
            className="absolute top-0 right-0 bottom-0 flex w-full max-w-5xl flex-col overflow-hidden border-l bg-background shadow-2xl"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="shrink-0 border-b bg-background">
              <div className="flex items-start justify-between gap-4 px-6 py-5">
                <div className="min-w-0 space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded-md border bg-muted/40 px-2 py-1 font-mono text-xs text-muted-foreground">
                      {selectedWebhookJob.transport}
                    </span>
                    <StatusBadge status={selectedWebhookJob.status} />
                  </div>
                  <div>
                    <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Webhook subject</p>
                    <h2 className="mt-1 break-all font-mono text-2xl font-semibold tracking-tight">{selectedWebhookJob.subject_key}</h2>
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {selectedWebhookJob.status === "failed" ? (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => void handleRetryWebhookJob(selectedWebhookJob.job_id)}
                      disabled={webhookBusy || retryingWebhookJobId === selectedWebhookJob.job_id}
                    >
                      {retryingWebhookJobId === selectedWebhookJob.job_id ? "Retrying..." : "Retry"}
                    </Button>
                  ) : null}
                  <Button variant="ghost" size="sm" className="h-8 w-8 px-0" onClick={() => setSelectedWebhookJobId(null)}>
                    <X className="h-4 w-4" />
                    <span className="sr-only">Close</span>
                  </Button>
                </div>
              </div>

              <div className="grid border-t bg-muted/20 sm:grid-cols-2 lg:grid-cols-4">
                {[
                  { label: "Attempts", value: String(selectedWebhookJob.attempt_count) },
                  { label: "Arrived", value: formatTimestamp(selectedWebhookJob.created_at, "—") },
                  { label: "Updated", value: formatTimestamp(selectedWebhookJob.updated_at, "—") },
                  { label: "Event", value: selectedWebhookJob.event_type ?? "—", mono: true },
                ].map((item) => (
                  <div key={item.label} className="border-b px-6 py-3 last:border-b-0 sm:border-r sm:last:border-r-0 lg:border-b-0">
                    <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{item.label}</p>
                    <p className={`mt-1 truncate text-sm ${item.mono ? "font-mono" : "font-medium"}`} title={item.value}>
                      {item.value}
                    </p>
                  </div>
                ))}
              </div>
            </div>

            <div className="min-h-0 flex-1 overflow-hidden">
              <div className="grid h-full min-h-0 lg:grid-cols-[minmax(0,1fr)_320px]">
                <section className="min-h-0 overflow-hidden border-r">
                  <div className="flex items-center justify-between gap-2 border-b px-6 py-3">
                    <h3 className="text-sm font-semibold">Failure detail</h3>
                    <span className="rounded-full border px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                      {selectedWebhookJob.last_error ? "Recorded" : "Empty"}
                    </span>
                  </div>
                  <div className="h-full min-h-0 overflow-auto bg-slate-950 p-6 text-slate-100">
                    <pre className="whitespace-pre-wrap break-words font-mono text-sm leading-6">
                      {selectedWebhookJob.last_error ?? "No error recorded."}
                    </pre>
                  </div>
                </section>

                <section className="min-h-0 overflow-y-auto bg-muted/10">
                  <div className="border-b px-5 py-4">
                    <h3 className="text-sm font-semibold">Job context</h3>
                  </div>
                  <dl className="divide-y text-sm">
                    {[
                      ["Job ID", selectedWebhookJob.job_id],
                      ["Request ID", selectedWebhookJob.request_id],
                      ["Dedupe key", selectedWebhookJob.dedupe_key ?? "—"],
                      ["Started", formatTimestamp(selectedWebhookJob.started_at, "—")],
                      ["Completed", formatTimestamp(selectedWebhookJob.completed_at, "—")],
                    ].map(([label, value]) => (
                      <div key={label} className="px-5 py-3">
                        <dt className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{label}</dt>
                        <dd className="mt-1 break-all font-mono text-xs text-foreground">{value}</dd>
                      </div>
                    ))}
                    <div className="px-5 py-3">
                      <dt className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Related run</dt>
                      <dd className="mt-1 break-all font-mono text-xs">
                        {selectedWebhookJob.related_run_id ? (
                          <Link
                            href={buildRunDetailPath({
                              tenantId: params.tenantId,
                              projectId: params.projectId,
                              runId: selectedWebhookJob.related_run_id,
                            })}
                            className="text-primary underline-offset-4 hover:underline"
                          >
                            {selectedWebhookJob.related_run_id}
                          </Link>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </dd>
                    </div>
                  </dl>
                </section>
              </div>
            </div>
          </aside>
        </div>
      ) : null}
    </div>
  );
}
