"use client";

import Link from "next/link";
import { useSearchParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, ArrowRight, CheckCircle2, KeyRound, Link2, RefreshCw } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  createTenant,
  getTenant,
  listGitHubRepositories,
  listJiraProjects,
  startGitHubInstall,
  startJiraConnect,
  updateTenant,
  type GitHubRepositoryRecord,
  type JiraProjectRecord
} from "@/lib/api";
import {
  defaultTenantFormValues,
  formValuesToTextFields,
  joinCsv,
  recordToFormValues,
  splitCsv,
  toCreatePayload,
  toUpdatePayload,
  type TenantFormTextFields,
  type TenantFormValues
} from "@/lib/tenant-form";

const STEP_ORDER = [
  { key: "basics", label: "Tenant Basics" },
  { key: "jira", label: "Connect Jira" },
  { key: "github", label: "Connect GitHub App" },
  { key: "repos", label: "Repositories + Policy" },
  { key: "review", label: "Review + Save" }
] as const;

const WIZARD_DRAFT_KEY = "mb_tenant_wizard_draft_v1";

export type WizardStepKey = (typeof STEP_ORDER)[number]["key"];

type WizardDraft = {
  values: TenantFormValues;
  textFields: TenantFormTextFields;
  createdTenantId: string;
  selectedRepoUrl: string;
};

function readWizardDraft(): Partial<WizardDraft> | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const raw = window.sessionStorage.getItem(WIZARD_DRAFT_KEY);
    if (!raw) {
      return null;
    }
    return JSON.parse(raw) as Partial<WizardDraft>;
  } catch {
    return null;
  }
}

function persistWizardDraft(draft: WizardDraft): void {
  if (typeof window === "undefined") {
    return;
  }
  window.sessionStorage.setItem(WIZARD_DRAFT_KEY, JSON.stringify(draft));
}

function previewTenantId(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function WizardProgress({ stepIndex }: { stepIndex: number }) {
  return (
    <ol className="grid gap-2 md:grid-cols-3">
      {STEP_ORDER.map((step, index) => {
        const active = index === stepIndex;
        const complete = index < stepIndex;
        return (
          <li
            key={step.key}
            className={`rounded-md border px-3 py-2 text-xs ${
              active ? "border-primary bg-primary/10 text-primary" : complete ? "border-emerald-300 bg-emerald-50" : ""
            }`}
          >
            <span className="font-semibold">{index + 1}.</span> {step.label}
          </li>
        );
      })}
    </ol>
  );
}

export function TenantSetupWizard({ stepKey }: { stepKey: WizardStepKey }) {
  const { credentials } = useAuth();
  const searchParams = useSearchParams();
  const router = useRouter();

  const [values, setValues] = useState<TenantFormValues>(() => {
    const draft = readWizardDraft();
    if (draft?.values) {
      return draft.values as TenantFormValues;
    }
    return defaultTenantFormValues();
  });
  const [textFields, setTextFields] = useState<TenantFormTextFields>(() => {
    const draft = readWizardDraft();
    if (draft?.textFields) {
      return draft.textFields as TenantFormTextFields;
    }
    if (draft?.values) {
      return formValuesToTextFields(draft.values as TenantFormValues);
    }
    return formValuesToTextFields(defaultTenantFormValues());
  });
  const [statusLine, setStatusLine] = useState("Start with tenant basics.");
  const [saving, setSaving] = useState(false);
  const [createdTenantId, setCreatedTenantId] = useState(() => {
    const draft = readWizardDraft();
    return typeof draft?.createdTenantId === "string" ? draft.createdTenantId : "";
  });

  const [installationRepos, setInstallationRepos] = useState<GitHubRepositoryRecord[]>([]);
  const [selectedRepoUrl, setSelectedRepoUrl] = useState(() => {
    const draft = readWizardDraft();
    return typeof draft?.selectedRepoUrl === "string" ? draft.selectedRepoUrl : "";
  });
  const [jiraProjects, setJiraProjects] = useState<JiraProjectRecord[]>([]);

  const stepIndex = STEP_ORDER.findIndex((step) => step.key === stepKey);

  const advanceValidationError = useMemo(() => {
    if (!values.name.trim()) {
      return "Tenant name is required.";
    }
    if (stepKey === "basics") {
      return null;
    }
    if (stepKey === "jira") {
      if (!values.jira.connection_id?.trim()) {
        return "Connect Jira before continuing.";
      }
      if (!textFields.projectKeysText.trim()) {
        return "Select at least one Jira project key before continuing.";
      }
      return null;
    }
    if (stepKey === "github") {
      if (!values.github.installation_id) {
        return "Install the GitHub App before continuing.";
      }
      return null;
    }
    if (stepKey === "repos") {
      if (!textFields.githubRepositoryText.trim()) {
        return "Select a GitHub repository before continuing.";
      }
      return null;
    }
    return null;
  }, [stepKey, textFields.githubRepositoryText, textFields.projectKeysText, values]);

  const canAdvance = advanceValidationError === null;

  useEffect(() => {
    const draft: WizardDraft = {
      values,
      textFields,
      createdTenantId,
      selectedRepoUrl
    };
    persistWizardDraft(draft);
  }, [values, textFields, createdTenantId, selectedRepoUrl]);

  useEffect(() => {
    const tenantIdParam = searchParams.get("tenant_id");
    if (!credentials || typeof tenantIdParam !== "string" || tenantIdParam.length === 0) {
      return;
    }
    const auth = credentials;
    const tenantId = tenantIdParam;

    let ignore = false;
    async function loadTenantFromQuery(): Promise<void> {
      try {
        const record = await getTenant(auth, tenantId);
        if (ignore) {
          return;
        }
        const form = recordToFormValues(record);
        setValues(form);
        setTextFields(formValuesToTextFields(form));
        setCreatedTenantId(record.tenant_id);
        setSelectedRepoUrl(form.repos.github_repository ?? "");
        if (searchParams.get("github_install") === "success") {
          setStatusLine("GitHub App install completed. Load repositories to continue setup.");
        } else {
          setStatusLine(`Loaded tenant ${record.tenant_id}.`);
        }
      } catch (error) {
        if (!ignore) {
          setStatusLine(`Failed to load tenant: ${(error as Error).message}`);
        }
      }
    }

    void loadTenantFromQuery();
    return () => {
      ignore = true;
    };
  }, [credentials, searchParams]);

  useEffect(() => {
    const connectionId = searchParams.get("jira_connection_id");
    if (!connectionId) {
      return;
    }
    setValues((prev) => ({ ...prev, jira: { ...prev.jira, connection_id: connectionId } }));
    if (searchParams.get("jira_oauth") === "success") {
      setStatusLine("Jira OAuth connected. Load Jira projects and select project keys.");
    }
  }, [searchParams]);

  async function ensureTenantCreated(): Promise<string | null> {
    if (!credentials) {
      setStatusLine("Missing API credentials.");
      return null;
    }
    if (!values.name.trim()) {
      setStatusLine("Tenant name is required.");
      return null;
    }
    if (createdTenantId) {
      return createdTenantId;
    }

    setSaving(true);
    try {
      const payload = toCreatePayload(values, textFields);
      const created = await createTenant(credentials, payload);
      const form = recordToFormValues(created);
      setValues(form);
      setTextFields(formValuesToTextFields(form));
      setCreatedTenantId(created.tenant_id);
      setStatusLine(`Created tenant ${created.tenant_id}.`);
      return created.tenant_id;
    } catch (error) {
      setStatusLine(`Create failed: ${(error as Error).message}`);
      return null;
    } finally {
      setSaving(false);
    }
  }

  async function handleStartJiraConnect() {
    if (!credentials) {
      return;
    }
    try {
      const result = await startJiraConnect(credentials, { returnTo: "wizard" });
      window.location.href = result.authorize_url;
    } catch (error) {
      setStatusLine(`Unable to start Jira OAuth: ${(error as Error).message}`);
    }
  }

  async function loadJiraProjectsForConnection() {
    if (!credentials || !values.jira.connection_id) {
      return;
    }
    try {
      const projects = await listJiraProjects(credentials, values.jira.connection_id);
      setJiraProjects(projects);
      if (projects.length === 0) {
        setStatusLine("Connected Jira site is valid, but no projects were returned.");
        return;
      }

      const existing = new Set(splitCsv(textFields.projectKeysText));
      const selected = projects.map((project) => project.key).filter((projectKey) => existing.has(projectKey));
      if (selected.length === 0) {
        setTextFields((prev) => ({
          ...prev,
          projectKeysText: projects.map((project) => project.key).join(", ")
        }));
      }

      setStatusLine(`Loaded ${projects.length} Jira project option(s).`);
    } catch (error) {
      setStatusLine(`Unable to load Jira projects: ${(error as Error).message}`);
    }
  }

  function toggleJiraProject(projectKey: string) {
    const current = new Set(splitCsv(textFields.projectKeysText));
    if (current.has(projectKey)) {
      current.delete(projectKey);
    } else {
      current.add(projectKey);
    }
    const ordered = jiraProjects.map((project) => project.key).filter((key) => current.has(key));
    const extras = [...current].filter((key) => !ordered.includes(key)).sort();
    setTextFields((prev) => ({ ...prev, projectKeysText: [...ordered, ...extras].join(", ") }));
  }

  async function handleStartInstall() {
    if (!credentials) {
      return;
    }
    const tenantId = await ensureTenantCreated();
    if (!tenantId) {
      return;
    }
    try {
      const result = await startGitHubInstall(credentials, tenantId, { returnTo: "wizard" });
      window.location.href = result.install_url;
    } catch (error) {
      setStatusLine(`Unable to start GitHub App install: ${(error as Error).message}`);
    }
  }

  async function loadInstallationRepositories() {
    if (!credentials || !createdTenantId) {
      return;
    }
    try {
      const repositories = await listGitHubRepositories(credentials, createdTenantId);
      setInstallationRepos(repositories);
      if (repositories.length === 0) {
        setStatusLine("GitHub installation connected, but no repositories are accessible.");
        return;
      }
      const existing = textFields.githubRepositoryText.trim();
      const selected = repositories.find((repo) => repo.html_url === existing);
      if (selected) {
        setSelectedRepoUrl(selected.html_url);
      } else {
        const defaultRepo = repositories[0]?.html_url ?? "";
        setSelectedRepoUrl(defaultRepo);
        setTextFields((prev) => ({ ...prev, githubRepositoryText: defaultRepo }));
      }
      setStatusLine(`Loaded ${repositories.length} repository option(s) from GitHub installation.`);
    } catch (error) {
      setStatusLine(`Unable to load repositories: ${(error as Error).message}`);
    }
  }

  useEffect(() => {
    if (stepKey !== "repos" || !values.github.installation_id || installationRepos.length > 0) {
      return;
    }
    void loadInstallationRepositories();
  }, [stepKey, values.github.installation_id, installationRepos.length]);

  async function handleSaveTenant() {
    if (!credentials) {
      return;
    }
    const tenantId = await ensureTenantCreated();
    if (!tenantId) {
      return;
    }

    setSaving(true);
    try {
      const payload = toUpdatePayload(values, textFields);
      const updated = await updateTenant(credentials, tenantId, payload);
      const form = recordToFormValues(updated);
      setValues(form);
      setTextFields(formValuesToTextFields(form));
      setStatusLine(`Saved tenant ${updated.tenant_id}.`);
    } catch (error) {
      setStatusLine(`Save failed: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  function goToStep(index: number) {
    persistWizardDraft({
      values,
      textFields,
      createdTenantId,
      selectedRepoUrl
    });
    const next = STEP_ORDER[index];
    router.push(`/tenants/new/${next.key}`);
  }

  async function nextStep() {
    if (advanceValidationError) {
      setStatusLine(advanceValidationError);
      return;
    }

    if (stepKey === "jira") {
      const tenantId = await ensureTenantCreated();
      if (!tenantId) {
        return;
      }
    }

    if (stepIndex < STEP_ORDER.length - 1) {
      goToStep(stepIndex + 1);
    }
  }

  function previousStep() {
    if (stepIndex > 0) {
      goToStep(stepIndex - 1);
    }
  }

  const selectedProjectKeys = new Set(splitCsv(textFields.projectKeysText));

  return (
    <Card>
      <CardHeader>
        <CardTitle>Tenant Setup Wizard</CardTitle>
        <CardDescription>
          Connect Jira and GitHub, then apply discovered projects and repositories into tenant policy.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <WizardProgress stepIndex={stepIndex} />
        <p className="rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          Manage credentials and webhook secrets in{" "}
          <Link href="/secrets" className="font-medium text-primary underline underline-offset-2">
            Secrets
          </Link>{" "}
          before running Jira/GitHub connection checks.
        </p>

        {stepKey === "basics" ? (
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Tenant Name</label>
              <Input
                value={values.name}
                onChange={(event) => setValues((prev) => ({ ...prev, name: event.target.value }))}
                placeholder="Tenant Demo"
              />
            </div>
            <div className="space-y-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Tenant ID Preview</label>
              <Input value={createdTenantId || previewTenantId(values.name)} disabled />
            </div>
          </div>
        ) : null}

        {stepKey === "jira" ? (
          <div className="space-y-3">
            <div className="rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">
              <p className="font-medium text-foreground">Before connecting Jira</p>
              <p>
                Save these secret refs in <strong>Secrets</strong>:{" "}
                <code className="font-mono">JIRA_OAUTH_CLIENT_ID</code>,{" "}
                <code className="font-mono">JIRA_OAUTH_CLIENT_SECRET</code>.
              </p>
            </div>
            <p className="rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
              <CheckCircle2 className="mr-1 inline h-4 w-4" />
              Jira connection: <strong>{values.jira.connection_id ?? "not connected"}</strong>
            </p>
            <div className="flex flex-wrap gap-2">
              <Button asChild variant="outline">
                <Link href="/secrets">
                  <KeyRound className="mr-2 h-4 w-4" />
                  Open Secrets
                </Link>
              </Button>
              <Button onClick={() => void handleStartJiraConnect()}>
                <Link2 className="mr-2 h-4 w-4" />
                Connect Jira
              </Button>
              <Button variant="outline" onClick={() => void loadJiraProjectsForConnection()} disabled={!values.jira.connection_id}>
                <RefreshCw className="mr-2 h-4 w-4" />
                Load Jira Projects
              </Button>
            </div>

            {jiraProjects.length > 0 ? (
              <div className="grid gap-2 md:grid-cols-2">
                {jiraProjects.map((project) => {
                  const selected = selectedProjectKeys.has(project.key);
                  return (
                    <label key={project.key} className="flex items-center gap-2 rounded-md border p-2 text-sm">
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={() => toggleJiraProject(project.key)}
                        className="h-4 w-4 rounded border-input"
                      />
                      <span>
                        <strong>{project.key}</strong> - {project.name}
                      </span>
                    </label>
                  );
                })}
              </div>
            ) : null}

            <div className="space-y-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Project Keys</label>
              <Input
                value={textFields.projectKeysText}
                onChange={(event) => setTextFields((prev) => ({ ...prev, projectKeysText: event.target.value }))}
                placeholder="TP, APP"
              />
            </div>
          </div>
        ) : null}

        {stepKey === "github" ? (
          <div className="space-y-3">
            <div className="rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">
              <p className="font-medium text-foreground">Before installing GitHub App</p>
              <p>
                Required secret ref in <strong>Secrets</strong>: <code className="font-mono">GITHUB_APP_SLUG</code>
              </p>
              <p>
                Required secret refs in <strong>Secrets</strong>:{" "}
                <code className="font-mono">GITHUB_APP_ID</code>,{" "}
                <code className="font-mono">GITHUB_CLIENT_SECRET</code>
              </p>
              <p>
                Optional webhook secret ref: <code className="font-mono">GITHUB_WEBHOOK_SECRET</code>
              </p>
            </div>
            <p className="rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
              <CheckCircle2 className="mr-1 inline h-4 w-4" />
              Tenant <strong>{createdTenantId || previewTenantId(values.name)}</strong> is ready for GitHub install.
            </p>
            <div className="flex flex-wrap gap-2">
              <Button asChild variant="outline">
                <Link href="/secrets">
                  <KeyRound className="mr-2 h-4 w-4" />
                  Open Secrets
                </Link>
              </Button>
              <Button onClick={() => void handleStartInstall()}>
                <Link2 className="mr-2 h-4 w-4" />
                Install GitHub App
              </Button>
              <Button variant="outline" onClick={() => void loadInstallationRepositories()} disabled={!values.github.installation_id}>
                <RefreshCw className="mr-2 h-4 w-4" />
                Load Repositories
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              Current installation ID: <strong>{values.github.installation_id ?? "not connected yet"}</strong>
            </p>
          </div>
        ) : null}

        {stepKey === "repos" ? (
          <div className="space-y-3">
            <div className="space-y-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Repository</label>
              <select
                className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                value={selectedRepoUrl}
                onChange={(event) => {
                  const url = event.target.value;
                  setSelectedRepoUrl(url);
                    setTextFields((prev) => ({ ...prev, githubRepositoryText: url }));
                }}
                disabled={installationRepos.length === 0}
              >
                <option value="">
                  {installationRepos.length === 0 ? "No repositories available" : "Select repository"}
                </option>
                {installationRepos.map((repo) => (
                  <option key={repo.html_url} value={repo.html_url}>
                    {repo.full_name}
                  </option>
                ))}
              </select>
            </div>
            <div className="grid gap-3 md:grid-cols-3">
              <div className="space-y-2">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Max runtime</label>
                <Input
                  type="number"
                  value={String(values.policy.max_runtime_minutes)}
                  onChange={(event) =>
                    setValues((prev) => ({
                      ...prev,
                      policy: { ...prev.policy, max_runtime_minutes: Number(event.target.value || 0) }
                    }))
                  }
                />
              </div>
              <div className="space-y-2">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Max loops</label>
                <Input
                  type="number"
                  value={String(values.policy.max_dev_test_review_loops)}
                  onChange={(event) =>
                    setValues((prev) => ({
                      ...prev,
                      policy: { ...prev.policy, max_dev_test_review_loops: Number(event.target.value || 0) }
                    }))
                  }
                />
              </div>
              <div className="space-y-2">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Max concurrency</label>
                <Input
                  type="number"
                  value={String(values.policy.max_concurrent_runs)}
                  onChange={(event) =>
                    setValues((prev) => ({
                      ...prev,
                      policy: { ...prev.policy, max_concurrent_runs: Number(event.target.value || 0) }
                    }))
                  }
                />
              </div>
            </div>
          </div>
        ) : null}

        {stepKey === "review" ? (
          <div className="space-y-3 text-sm">
            <p className="rounded-md border bg-muted/30 p-3">
              <strong>Tenant:</strong> {createdTenantId || previewTenantId(values.name)} ({values.name || "-"})
            </p>
            <p className="rounded-md border bg-muted/30 p-3">
              <strong>Jira connection:</strong> {values.jira.connection_id || "-"} | keys:{" "}
              {joinCsv(splitCsv(textFields.projectKeysText)) || "-"}
            </p>
            <p className="rounded-md border bg-muted/30 p-3">
              <strong>GitHub installation:</strong> {values.github.installation_id || "not connected"}
            </p>
            <p className="rounded-md border bg-muted/30 p-3">
              <strong>Repository:</strong> {textFields.githubRepositoryText || "-"}
            </p>
            <div className="flex gap-2">
              <Button onClick={() => void handleSaveTenant()} disabled={saving}>
                {saving ? "Saving..." : "Save Tenant"}
              </Button>
              {createdTenantId ? (
                <Button variant="outline" asChild>
                  <Link href={`/tenants/${encodeURIComponent(createdTenantId)}/edit`}>Open Tenant Editor</Link>
                </Button>
              ) : null}
            </div>
          </div>
        ) : null}

        <div className="flex items-center justify-between gap-2 border-t pt-3">
          <Button variant="outline" onClick={previousStep} disabled={stepIndex === 0}>
            <ArrowLeft className="mr-2 h-4 w-4" /> Back
          </Button>
          <p className="text-xs text-muted-foreground">{statusLine}</p>
          <Button onClick={() => void nextStep()} disabled={stepIndex >= STEP_ORDER.length - 1 || saving}>
            Next <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
