"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import {
  Archive,
  ArrowLeft,
  ExternalLink,
  KeyRound,
  Library,
  Plus,
  RefreshCw,
  SlidersHorizontal,
  Trash2,
} from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { ProjectNotificationsContent } from "@/components/tenant-project-discord-page";
import { CodexModelSelect } from "@/components/codex-model-select";
import { OverrideSegmentedControl } from "@/components/override-segmented-control";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import {
  getProject,
  getTenant,
  listCodexModels,
  listGitHubRepositories,
  listJiraProjects,
  listRuns,
  RUN_STATUSES,
  updateProject,
  type ProjectRecord,
  type RunRecord,
  type RunStatus,
} from "@/lib/api";

type Tab = "overview" | "settings" | "runs" | "notifications" | "secrets";
type SettingsSection = "general" | "ai" | "automation" | "knowledge" | "governance";
type OverrideToggleValue = "inherit" | "enabled" | "disabled";
type RequireAgentsValue = "inherit" | "required";
type KnowledgeModeValue = "inherit" | "safe" | "balanced" | "aggressive";
type AllowedCommandsMode = "inherit" | "custom";

type ProjectFormState = {
  name: string;
  github_repository: string;
  jira_project_key: string;
  codex_model: string | null;
  codex_reasoning_effort: "low" | "medium" | "high" | null;
  allow_jira_transitions: OverrideToggleValue;
  allow_pr_creation: OverrideToggleValue;
  allow_pr_remediation: OverrideToggleValue;
  allow_label_mutations: OverrideToggleValue;
  allow_auto_merge: OverrideToggleValue;
  require_agents_md: RequireAgentsValue;
  knowledge_base_enabled: OverrideToggleValue;
  knowledge_auto_answer_mode: KnowledgeModeValue;
  max_dev_test_review_loops: string;
  max_pr_auto_remediation_loops: string;
  max_concurrent_runs: string;
  allowed_commands_mode: AllowedCommandsMode;
  allowed_commands_text: string;
};

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "settings", label: "Settings" },
  { id: "runs", label: "Runs" },
  { id: "notifications", label: "Notifications" },
  { id: "secrets", label: "Secrets" },
];

const SETTINGS_SECTIONS: { id: SettingsSection; label: string; description: string }[] = [
  { id: "general", label: "General", description: "Name, repository, and Jira mapping." },
  { id: "ai", label: "AI", description: "Model and reasoning controls." },
  { id: "automation", label: "Automation", description: "Execution, PR, and command policy." },
  { id: "knowledge", label: "Knowledge", description: "Knowledge-base behavior for this project." },
  { id: "governance", label: "Governance", description: "Repository standards and archive controls." },
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
    codex_model: typeof overrides.codex_model === "string" ? overrides.codex_model : null,
    codex_reasoning_effort:
      overrides.codex_reasoning_effort === "low" ||
      overrides.codex_reasoning_effort === "medium" ||
      overrides.codex_reasoning_effort === "high"
        ? overrides.codex_reasoning_effort
        : null,
    allow_jira_transitions: booleanOverrideToState(overrides.allow_jira_transitions),
    allow_pr_creation: booleanOverrideToState(overrides.allow_pr_creation),
    allow_pr_remediation: booleanOverrideToState(overrides.allow_pr_remediation),
    allow_label_mutations: booleanOverrideToState(overrides.allow_label_mutations),
    allow_auto_merge: booleanOverrideToState(overrides.allow_auto_merge),
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

export function TenantProjectDetailsPage() {
  const params = useParams<{ tenantId: string; projectId: string }>();
  const { credentials, ready } = useAuth();

  const [activeTab, setActiveTab] = useState<Tab>("overview");
  const [activeSettingsSection, setActiveSettingsSection] = useState<SettingsSection>("general");

  // Project state
  const [project, setProject] = useState<ProjectRecord | null>(null);
  const [form, setForm] = useState<ProjectFormState>(() => buildProjectFormState(null));
  const [busy, setBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [repoOptions, setRepoOptions] = useState<string[]>([]);
  const [jiraOptions, setJiraOptions] = useState<string[]>([]);
  const [codexModels, setCodexModels] = useState<{ id: string; label: string; description?: string | null }[]>([]);
  const [globalCodexModel, setGlobalCodexModel] = useState("");
  const [reasoningEfforts, setReasoningEfforts] = useState<{ id: string; label: string; description?: string | null }[]>([]);
  const [globalCodexReasoningEffort, setGlobalCodexReasoningEffort] = useState("");

  // Runs state
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [runsBusy, setRunsBusy] = useState(false);
  const [runIssueFilter, setRunIssueFilter] = useState("");
  const [runStatusFilter, setRunStatusFilter] = useState<RunStatus | "all">("all");
  const [runPrFilter, setRunPrFilter] = useState<"any" | "none" | "has_value">("any");
  const [runFromDate, setRunFromDate] = useState("");
  const [runToDate, setRunToDate] = useState("");
  const [runPage, setRunPage] = useState(1);
  const [runPageSize, setRunPageSize] = useState(25);

  // Secrets state
  const [secretRefs, setSecretRefs] = useState<Record<string, string>>({});
  const [newSecretKey, setNewSecretKey] = useState("");
  const [newSecretRef, setNewSecretRef] = useState("");
  const [secretsBusy, setSecretsBusy] = useState(false);
  const [secretsStatusLine, setSecretsStatusLine] = useState("");

  async function loadOptions() {
    if (!credentials) return;
    try {
      const tenant = await getTenant(credentials, params.tenantId);
      const modelCatalog = await listCodexModels(credentials);
      setCodexModels(modelCatalog.models);
      setGlobalCodexModel(modelCatalog.default_model);
      setReasoningEfforts(modelCatalog.reasoning_efforts);
      setGlobalCodexReasoningEffort(modelCatalog.default_reasoning_effort);
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
    } finally {
      setRunsBusy(false);
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
      await loadOptions();
      await loadRuns();
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to load project: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) void loadProject();
  }, [ready, credentials, params.tenantId, params.projectId]);

  useEffect(() => {
    if (ready && credentials && project) void loadRuns();
  }, [ready, credentials, project, runPage, runPageSize]);

  async function toggleArchive() {
    if (!credentials || !project) return;
    setBusy(true);
    try {
      const updated = await updateProject(credentials, params.tenantId, params.projectId, {
        name: form.name.trim(),
        github_repository: form.github_repository.trim(),
        jira_project_key: form.jira_project_key.trim().toUpperCase(),
        policy_overrides: project.policy_overrides,
        environment: project.environment,
        secret_refs: secretRefs,
        discord: project.discord,
        is_archived: !project.is_archived,
      });
      setProject(updated);
      setForm(buildProjectFormState(updated));
      setStatusLine(updated.is_archived ? "Project archived." : "Project unarchived.");
    } catch (error) {
      setStatusLine(`Unable to update project: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function saveDetails() {
    if (!credentials || !project) return;
    if (!form.name.trim() || !form.github_repository.trim() || !form.jira_project_key.trim()) {
      setStatusLine("Project name, repository, and Jira key are required.");
      return;
    }
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
    const allowPrRemediation = booleanStateToOverride(form.allow_pr_remediation);
    const allowLabelMutations = booleanStateToOverride(form.allow_label_mutations);
    const allowAutoMerge = booleanStateToOverride(form.allow_auto_merge);
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
    if (allowPrRemediation === undefined) {
      delete nextPolicyOverrides.allow_pr_remediation;
    } else {
      nextPolicyOverrides.allow_pr_remediation = allowPrRemediation;
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
    setBusy(true);
    try {
      const updated = await updateProject(credentials, params.tenantId, params.projectId, {
        name: form.name.trim(),
        github_repository: form.github_repository.trim(),
        jira_project_key: form.jira_project_key.trim().toUpperCase(),
        policy_overrides: nextPolicyOverrides,
        environment: project.environment,
        secret_refs: secretRefs,
        discord: project.discord,
        is_archived: project.is_archived,
      });
      setProject(updated);
      setForm(buildProjectFormState(updated));
      setStatusLine("Project details saved.");
    } catch (error) {
      setStatusLine(`Unable to update project: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function saveSecretRefs(nextSecretRefs?: Record<string, string>) {
    if (!credentials || !project) return;
    const refsToSave = nextSecretRefs ?? secretRefs;
    setSecretsBusy(true);
    try {
      const updated = await updateProject(credentials, params.tenantId, params.projectId, {
        name: project.name,
        github_repository: project.github_repository,
        jira_project_key: project.jira_project_key,
        policy_overrides: project.policy_overrides,
        environment: project.environment,
        secret_refs: refsToSave,
        discord: project.discord,
        is_archived: project.is_archived,
      });
      setProject(updated);
      setSecretRefs(updated.secret_refs ?? {});
      setSecretsStatusLine("Project secrets saved as managed refs.");
    } catch (error) {
      setSecretsStatusLine(`Save failed: ${(error as Error).message}`);
    } finally {
      setSecretsBusy(false);
    }
  }

  async function addSecretRef() {
    const key = newSecretKey.trim();
    const ref = newSecretRef.trim();
    if (!key || !ref) {
      setSecretsStatusLine("Both variable name and a secret value or existing secret ref are required.");
      return;
    }
    const nextRefs = { ...secretRefs, [key]: ref };
    setSecretRefs(nextRefs);
    setNewSecretKey("");
    setNewSecretRef("");
    setSecretsStatusLine("");
    await saveSecretRefs(nextRefs);
  }

  async function removeSecretRef(key: string) {
    const nextRefs = { ...secretRefs };
    delete nextRefs[key];
    setSecretRefs(nextRefs);
    setSecretsStatusLine("");
    await saveSecretRefs(nextRefs);
  }

  return (
    <div className="space-y-6">
      {/* Header strip */}
      <div className="flex flex-wrap items-center gap-3">
        <Button asChild variant="ghost" size="sm" className="-ml-1">
          <Link href={`/tenants/${encodeURIComponent(params.tenantId)}/projects`}>
            <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />
            Back
          </Link>
        </Button>
        <div className="flex items-center gap-2.5 min-w-0">
          <h1 className="truncate text-xl font-semibold">{project?.name ?? params.projectId}</h1>
          {project ? (
            <Badge variant={project.is_archived ? "outline" : "success"} className="shrink-0">
              {project.is_archived ? "Archived" : "Active"}
            </Badge>
          ) : null}
        </div>
      </div>

      {/* Underline tab bar */}
      <div className="border-b">
        <nav className="flex gap-1 -mb-px">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`px-4 py-2.5 text-sm font-medium transition-colors whitespace-nowrap border-b-2 ${
                activeTab === tab.id
                  ? "border-primary text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground"
              }`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {/* ── Overview tab ─────────────────────────────────────────────────── */}
      {activeTab === "overview" ? (
        <div className="space-y-4">
          {statusLine ? (
            <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p>
          ) : null}
          {project ? (
            <>
              <div className="grid gap-4 xl:grid-cols-[1.2fr,0.8fr]">
                <Card>
                  <CardHeader className="pb-3">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <CardTitle className="text-base">Project overview</CardTitle>
                        <p className="text-sm text-muted-foreground">
                          Core project identity and the main places operators will go next.
                        </p>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <Button size="sm" onClick={() => setActiveTab("settings")}>
                          Open settings
                        </Button>
                        <Button asChild size="sm" variant="outline">
                          <Link href={`/tenants/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(params.projectId)}/knowledge`}>
                            Browse knowledge
                          </Link>
                        </Button>
                      </div>
                    </div>
                  </CardHeader>
                  <CardContent className="grid gap-4 md:grid-cols-2">
                    <div className="space-y-1">
                      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Project name</p>
                      <p className="text-sm font-medium text-foreground">{project.name}</p>
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Status</p>
                      <p className="text-sm font-medium text-foreground">{project.is_archived ? "Archived" : "Active"}</p>
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Repository</p>
                      <Link
                        className="inline-flex items-center gap-1 text-sm text-primary hover:underline"
                        href={project.github_repository}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {project.github_repository}
                        <ExternalLink className="h-3 w-3" />
                      </Link>
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Jira project</p>
                      <p className="text-sm font-medium text-foreground">{project.jira_project_key}</p>
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Secret refs</p>
                      <p className="text-sm font-medium text-foreground">{Object.keys(secretRefs).length}</p>
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Loaded runs</p>
                      <p className="text-sm font-medium text-foreground">{runs.length}</p>
                    </div>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader className="pb-3">
                    <CardTitle className="text-base">Quick navigation</CardTitle>
                  </CardHeader>
                  <CardContent className="grid gap-2 sm:grid-cols-2">
                    <Button variant="outline" size="sm" onClick={() => setActiveTab("settings")}>
                      Settings
                    </Button>
                    <Button asChild variant="outline" size="sm">
                      <Link href={`/tenants/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(params.projectId)}/knowledge`}>
                        Browse knowledge
                      </Link>
                    </Button>
                    <Button asChild variant="outline" size="sm">
                      <Link href={`/tenants/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(params.projectId)}/knowledge?view=add`}>
                        Add knowledge
                      </Link>
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => setActiveTab("notifications")}>
                      Notifications
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => setActiveTab("secrets")}>
                      Secrets
                    </Button>
                  </CardContent>
                </Card>
              </div>

              <div className="grid gap-4 xl:grid-cols-3">
                <Card className="xl:col-span-1">
                  <CardHeader className="pb-3">
                    <CardTitle className="text-base">Effective AI policy</CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <div className="space-y-1">
                      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Model</p>
                      <p className="text-sm font-medium text-foreground">
                        {project.effective_policy.codex_model ?? (globalCodexModel || "Global default")}
                      </p>
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Reasoning mode</p>
                      <p className="text-sm font-medium text-foreground">
                        {project.effective_policy.codex_reasoning_effort ?? (globalCodexReasoningEffort || "medium")}
                      </p>
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Knowledge base</p>
                      <p className="text-sm font-medium text-foreground">
                        {formatBoolean(project.effective_policy.knowledge_base_enabled)}
                      </p>
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Knowledge answer mode</p>
                      <p className="text-sm font-medium text-foreground">{project.effective_policy.knowledge_auto_answer_mode}</p>
                    </div>
                  </CardContent>
                </Card>

                <Card className="xl:col-span-1">
                  <CardHeader className="pb-3">
                    <CardTitle className="text-base">Effective automation policy</CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <div className="grid gap-3 sm:grid-cols-2">
                      <div className="space-y-1">
                        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">PR creation</p>
                        <p className="text-sm font-medium text-foreground">{formatBoolean(project.effective_policy.allow_pr_creation)}</p>
                      </div>
                      <div className="space-y-1">
                        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">PR remediation</p>
                        <p className="text-sm font-medium text-foreground">{formatBoolean(project.effective_policy.allow_pr_remediation)}</p>
                      </div>
                      <div className="space-y-1">
                        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Auto merge</p>
                        <p className="text-sm font-medium text-foreground">{formatBoolean(project.effective_policy.allow_auto_merge)}</p>
                      </div>
                      <div className="space-y-1">
                        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Label mutations</p>
                        <p className="text-sm font-medium text-foreground">{formatBoolean(project.effective_policy.allow_label_mutations)}</p>
                      </div>
                      <div className="space-y-1">
                        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Jira transitions</p>
                        <p className="text-sm font-medium text-foreground">{formatBoolean(project.effective_policy.allow_jira_transitions)}</p>
                      </div>
                    </div>
                    <div className="grid gap-3 sm:grid-cols-3">
                      <div className="space-y-1">
                        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Dev/test/review loops</p>
                        <p className="text-sm font-medium text-foreground">{project.effective_policy.max_dev_test_review_loops}</p>
                      </div>
                      <div className="space-y-1">
                        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">PR remediation loops</p>
                        <p className="text-sm font-medium text-foreground">{project.effective_policy.max_pr_auto_remediation_loops}</p>
                      </div>
                      <div className="space-y-1">
                        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Concurrent runs</p>
                        <p className="text-sm font-medium text-foreground">{project.effective_policy.max_concurrent_runs}</p>
                      </div>
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Allowed commands</p>
                      <p className="text-sm text-foreground">
                        {project.effective_policy.allowed_commands.length > 0
                          ? project.effective_policy.allowed_commands.join(", ")
                          : "None"}
                      </p>
                    </div>
                  </CardContent>
                </Card>

                <Card className="xl:col-span-1">
                  <CardHeader className="pb-3">
                    <div className="flex items-center gap-2">
                      <Library className="h-4 w-4 text-primary" />
                      <CardTitle className="text-base">Knowledge</CardTitle>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <div className="space-y-1">
                      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Knowledge browser</p>
                      <p className="text-sm text-muted-foreground">
                        Inspect indexed assets, metadata, and retrieval chunks from the dedicated browser page.
                      </p>
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Add knowledge</p>
                      <p className="text-sm text-muted-foreground">
                        Upload files directly into the knowledge store from the Add Knowledge tab.
                      </p>
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Sources</p>
                      <p className="text-sm text-muted-foreground">
                        Manage Jira, Google Drive, and Discord connectors from the Sources tab.
                      </p>
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">AGENTS.md requirement</p>
                      <p className="text-sm font-medium text-foreground">
                        {project.effective_policy.require_agents_md ? "Required" : "Not required"}
                      </p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Button asChild size="sm" variant="outline">
                        <Link href={`/tenants/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(params.projectId)}/knowledge`}>
                          Browse knowledge
                        </Link>
                      </Button>
                      <Button asChild size="sm" variant="outline">
                        <Link href={`/tenants/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(params.projectId)}/knowledge?view=add`}>
                          Add knowledge
                        </Link>
                      </Button>
                      <Button asChild size="sm" variant="outline">
                        <Link href={`/tenants/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(params.projectId)}/knowledge?view=sources`}>
                          Sources
                        </Link>
                      </Button>
                    </div>
                    <div className="space-y-1">
                      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Secrets</p>
                      <p className="text-sm text-muted-foreground">
                        Runtime secret references are configured separately to keep settings focused.
                      </p>
                    </div>
                  </CardContent>
                </Card>
              </div>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">Loading project details…</p>
          )}
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
              <Card>
                <CardHeader className="pb-3">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <CardTitle className="text-base">Settings</CardTitle>
                      <p className="text-sm text-muted-foreground">
                        Project-level overrides for execution, automation, and AI behavior.
                      </p>
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
                </CardHeader>
                <CardContent className="space-y-4">
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
                        <p className="mt-1 text-xs text-muted-foreground">{section.description}</p>
                      </button>
                    ))}
                  </div>
                </CardContent>
              </Card>

              {activeSettingsSection === "general" ? (
                <Card>
                  <CardHeader>
                    <CardTitle className="text-base">General</CardTitle>
                  </CardHeader>
                  <CardContent className="grid gap-4 md:grid-cols-3">
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
                    </div>
                  </CardContent>
                </Card>
              ) : null}

              {activeSettingsSection === "ai" ? (
                <Card>
                  <CardHeader>
                    <CardTitle className="text-base">AI</CardTitle>
                  </CardHeader>
                  <CardContent className="grid gap-4 md:grid-cols-2">
                    <div className="space-y-1.5 md:col-span-2">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Codex model override
                      </label>
                      <CodexModelSelect
                        value={form.codex_model}
                        models={codexModels}
                        inheritLabel="Inherit tenant model"
                        effectiveLabel={`Effective model: ${project.effective_policy.codex_model ?? (globalCodexModel || "global default")}`}
                        helperText={globalCodexModel ? `Global default: ${globalCodexModel}` : undefined}
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
                        onChange={(e) =>
                          setForm((prev) => ({
                            ...prev,
                            codex_reasoning_effort: (e.target.value || null) as ProjectFormState["codex_reasoning_effort"],
                          }))
                        }
                        disabled={busy}
                      >
                        <option value="">Inherit tenant reasoning mode</option>
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
                  </CardContent>
                </Card>
              ) : null}

              {activeSettingsSection === "automation" ? (
                <Card>
                  <CardHeader>
                    <CardTitle className="text-base">Automation</CardTitle>
                  </CardHeader>
                  <CardContent className="grid gap-4 md:grid-cols-2">
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
                        Effective commands: {project.effective_policy.allowed_commands.length > 0 ? project.effective_policy.allowed_commands.join(", ") : "none"}
                      </p>
                    </div>
                  </CardContent>
                </Card>
              ) : null}

              {activeSettingsSection === "knowledge" ? (
                <Card>
                  <CardHeader>
                    <CardTitle className="text-base">Knowledge</CardTitle>
                  </CardHeader>
                  <CardContent className="grid gap-4 md:grid-cols-2">
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
                  </CardContent>
                </Card>
              ) : null}

              {activeSettingsSection === "governance" ? (
                <div className="space-y-4">
                  <Card>
                    <CardHeader>
                      <CardTitle className="text-base">Governance</CardTitle>
                    </CardHeader>
                    <CardContent className="grid gap-4 md:grid-cols-2">
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
                    </CardContent>
                  </Card>

                  <Card className="border-warning/40">
                    <CardHeader>
                      <CardTitle className="text-base">Archive</CardTitle>
                    </CardHeader>
                    <CardContent className="flex flex-wrap items-center justify-between gap-3">
                      <p className="text-sm text-muted-foreground">
                        Archive this project to stop treating it as an active workspace without deleting its history.
                      </p>
                      <Button variant="outline" size="sm" onClick={() => void toggleArchive()} disabled={busy}>
                        <Archive className="mr-1.5 h-3.5 w-3.5" />
                        {project.is_archived ? "Unarchive" : "Archive"}
                      </Button>
                    </CardContent>
                  </Card>
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
        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between gap-2">
              <CardTitle className="text-base">Project Runs</CardTitle>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => { setRunPage(1); void loadRuns(); }}
                disabled={runsBusy}
              >
                <RefreshCw className={`h-3.5 w-3.5 ${runsBusy ? "animate-spin" : ""}`} />
              </Button>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            {/* Filter toolbar */}
            <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-muted/30 px-3 py-2.5">
              <SlidersHorizontal className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
              <Input
                className="h-8 w-40 text-sm"
                value={runIssueFilter}
                onChange={(e) => setRunIssueFilter(e.target.value)}
                placeholder="Issue / summary"
                disabled={runsBusy}
              />
              <select
                className="h-8 w-36 rounded-md border border-input bg-background px-2 text-sm"
                value={runStatusFilter}
                onChange={(e) => setRunStatusFilter(e.target.value as RunStatus | "all")}
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
                onChange={(e) => setRunPrFilter(e.target.value as "any" | "none" | "has_value")}
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
                onChange={(e) => setRunFromDate(e.target.value)}
                disabled={runsBusy}
              />
              <Input
                type="date"
                className="h-8 w-36 text-sm"
                value={runToDate}
                onChange={(e) => setRunToDate(e.target.value)}
                disabled={runsBusy}
              />
              <select
                className="h-8 rounded-md border border-input bg-background px-2 text-sm"
                value={String(runPageSize)}
                onChange={(e) => { setRunPageSize(Number(e.target.value)); setRunPage(1); }}
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
                onClick={() => { setRunPage(1); void loadRuns(); }}
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

            {runs.length === 0 ? (
              <p className="rounded-lg border bg-muted/30 px-4 py-8 text-center text-sm text-muted-foreground">
                No runs found for this project.
              </p>
            ) : (
              <div className="overflow-hidden rounded-lg border">
                <Table>
                  <TableHeader>
                    <TableRow className="bg-muted/40">
                      <TableHead className="pl-4">Run</TableHead>
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
                        <TableCell className="pl-4 font-medium">
                          <Link
                            className="text-primary hover:underline"
                            href={`/tenants/${encodeURIComponent(params.tenantId)}/runs/${encodeURIComponent(run.run_id)}`}
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
                          {new Date(run.created_at).toLocaleString()}
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
              </div>
            )}

            {/* Pagination */}
            <div className="flex items-center justify-end gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setRunPage((p) => Math.max(1, p - 1))}
                disabled={runsBusy || runPage <= 1}
              >
                ← Prev
              </Button>
              <span className="text-sm text-muted-foreground">Page {runPage}</span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setRunPage((p) => p + 1)}
                disabled={runsBusy || runs.length < runPageSize}
              >
                Next →
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {/* ── Notifications tab ────────────────────────────────────────────── */}
      {activeTab === "notifications" ? (
        <ProjectNotificationsContent
          tenantId={params.tenantId}
          projectId={params.projectId}
          credentials={credentials}
        />
      ) : null}

      {/* ── Secrets tab ──────────────────────────────────────────────────── */}
      {activeTab === "secrets" ? (
        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2.5">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10">
                <KeyRound className="h-4 w-4 text-primary" />
              </div>
              <div>
                <CardTitle className="text-base">Project Secrets</CardTitle>
                <p className="text-sm text-muted-foreground">
                  Map variable names to secret values or existing managed refs. Secret values are stored in the managed
                  secret provider and persisted here as refs for runtime resolution.
                </p>
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            {secretsStatusLine ? (
              <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">
                {secretsStatusLine}
              </p>
            ) : null}

            {/* Add row */}
            <div className="flex flex-wrap items-end gap-2 rounded-lg border bg-muted/30 px-4 py-3">
              <div className="space-y-1 flex-1 min-w-32">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Variable name
                </label>
                <Input
                  className="h-8 text-sm font-mono"
                  value={newSecretKey}
                  onChange={(e) => setNewSecretKey(e.target.value)}
                  placeholder="e.g. OPENAI_KEY"
                  disabled={secretsBusy}
                />
              </div>
              <div className="space-y-1 flex-[2] min-w-48">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Secret value or ref
                </label>
                <Input
                  className="h-8 text-sm font-mono"
                  value={newSecretRef}
                  onChange={(e) => setNewSecretRef(e.target.value)}
                  placeholder="e.g. platform/OPENAI_KEY or an actual secret value"
                  disabled={secretsBusy}
                />
              </div>
              <Button size="sm" onClick={() => void addSecretRef()} disabled={secretsBusy} className="h-8">
                <Plus className="mr-1.5 h-3.5 w-3.5" />
                Add
              </Button>
            </div>

            {/* Existing entries */}
            {Object.keys(secretRefs).length === 0 ? (
              <div className="flex flex-col items-center justify-center gap-2 rounded-lg border bg-muted/20 py-8 text-center">
                <KeyRound className="h-6 w-6 text-muted-foreground" />
                <p className="text-sm font-medium">No secret references configured</p>
                <p className="text-xs text-muted-foreground">
                  Add a variable name and secret value or ref above to get started.
                </p>
              </div>
            ) : (
              <div className="overflow-hidden rounded-lg border">
                <table className="w-full text-sm">
                  <thead className="bg-muted/40">
                    <tr>
                      <th className="px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Variable name
                      </th>
                      <th className="px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Secret ref path
                      </th>
                      <th className="px-4 py-2.5 text-right text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Actions
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {Object.entries(secretRefs).map(([key, ref]) => (
                      <tr key={key} className="group">
                        <td className="px-4 py-3 font-mono text-xs font-medium">{key}</td>
                        <td className="px-4 py-3 font-mono text-xs text-muted-foreground">{ref}</td>
                        <td className="px-4 py-3 text-right">
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-7 w-7 p-0 opacity-60 hover:opacity-100 hover:text-destructive"
                            onClick={() => void removeSecretRef(key)}
                            disabled={secretsBusy}
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Save footer */}
            <div className="flex items-center justify-end border-t pt-4">
              <Button size="sm" onClick={() => void saveSecretRefs()} disabled={secretsBusy || !project}>
                {secretsBusy ? "Saving…" : "Save secrets"}
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
