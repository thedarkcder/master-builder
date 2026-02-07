"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, ArrowRight, CheckCircle2, Link2, RefreshCw } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  createTenant,
  getTenant,
  listGitHubRepositories,
  startGitHubInstall,
  updateTenant,
  type GitHubRepositoryRecord
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

const STEPS = [
  "Tenant Basics",
  "Jira",
  "Connect GitHub App",
  "Repositories + Policy",
  "Review + Save"
] as const;

function previewTenantId(name: string): string {
  const slug = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "tenant";
}

function WizardProgress({ step }: { step: number }) {
  return (
    <ol className="grid gap-2 md:grid-cols-3">
      {STEPS.map((label, index) => {
        const active = index === step;
        const complete = index < step;
        return (
          <li
            key={label}
            className={`rounded-md border px-3 py-2 text-xs ${
              active ? "border-primary bg-primary/10 text-primary" : complete ? "border-emerald-300 bg-emerald-50" : ""
            }`}
          >
            <span className="font-semibold">{index + 1}.</span> {label}
          </li>
        );
      })}
    </ol>
  );
}

export default function NewTenantPage() {
  const { credentials } = useAuth();
  const searchParams = useSearchParams();

  const [values, setValues] = useState<TenantFormValues>(defaultTenantFormValues());
  const [textFields, setTextFields] = useState<TenantFormTextFields>(formValuesToTextFields(defaultTenantFormValues()));
  const [step, setStep] = useState(0);
  const [statusLine, setStatusLine] = useState("Start with tenant basics.");
  const [saving, setSaving] = useState(false);
  const [createdTenantId, setCreatedTenantId] = useState("");
  const [installationRepos, setInstallationRepos] = useState<GitHubRepositoryRecord[]>([]);
  const [selectedRepoUrl, setSelectedRepoUrl] = useState("");

  const canAdvance = useMemo(() => {
    if (step === 0) {
      return Boolean(values.name.trim());
    }
    if (step === 1) {
      return Boolean(values.jira.mcp_endpoint.trim() && textFields.projectKeysText.trim());
    }
    if (step === 2) {
      return Boolean(values.github.installation_id);
    }
    if (step === 3) {
      return Boolean(textFields.allowlistText.trim());
    }
    return true;
  }, [step, textFields.allowlistText, textFields.projectKeysText, values]);

  useEffect(() => {
    const tenantIdParam = searchParams.get("tenant_id");
    if (!credentials || !tenantIdParam) {
      return;
    }
    const activeCredentials = credentials;
    const tenantId = tenantIdParam;

    let ignore = false;
    async function loadTenantFromQuery(): Promise<void> {
      try {
        const record = await getTenant(activeCredentials, tenantId);
        if (ignore) {
          return;
        }
        const form = recordToFormValues(record);
        setValues(form);
        setTextFields(formValuesToTextFields(form));
        setCreatedTenantId(record.tenant_id);
        if (searchParams.get("github_install") === "success") {
          setStep(2);
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

  async function ensureTenantCreated(): Promise<string | null> {
    if (!credentials) {
      setStatusLine("Missing API credentials.");
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
      setStatusLine(`Created tenant ${created.tenant_id}. Connect GitHub App next.`);
      return created.tenant_id;
    } catch (error) {
      setStatusLine(`Create failed: ${(error as Error).message}`);
      return null;
    } finally {
      setSaving(false);
    }
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
      const preferred = repositories[0];
      setSelectedRepoUrl(preferred.html_url);
      setStatusLine(`Loaded ${repositories.length} repository option(s) from GitHub installation.`);
    } catch (error) {
      setStatusLine(`Unable to load repositories: ${(error as Error).message}`);
    }
  }

  function applyRepositorySelection() {
    if (!selectedRepoUrl) {
      setStatusLine("Pick a repository first.");
      return;
    }

    const projectKeys = splitCsv(textFields.projectKeysText);
    const mappings = projectKeys.map((projectKey) => `${projectKey}=${selectedRepoUrl}`).join("\n");
    setTextFields((prev) => ({
      ...prev,
      allowlistText: selectedRepoUrl,
      mappingByProjectText: mappings
    }));
    setValues((prev) => ({
      ...prev,
      repos: {
        ...prev.repos,
        fallback_repo: selectedRepoUrl
      }
    }));

    const selectedRepo = installationRepos.find((repo) => repo.html_url === selectedRepoUrl);
    if (selectedRepo && !values.name.trim()) {
      const repoName = selectedRepo.full_name.split("/").at(-1) ?? selectedRepo.full_name;
      setValues((prev) => ({ ...prev, name: repoName }));
    }
    setStatusLine("Repository settings applied from GitHub selection.");
  }

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

  async function nextStep() {
    if (!canAdvance && step !== 1) {
      setStatusLine("Please complete required fields before continuing.");
      return;
    }

    if (step === 1) {
      const tenantId = await ensureTenantCreated();
      if (!tenantId) {
        return;
      }
      setStep(2);
      return;
    }
    setStep((current) => Math.min(current + 1, STEPS.length - 1));
  }

  function previousStep() {
    setStep((current) => Math.max(current - 1, 0));
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Tenant Setup Wizard</CardTitle>
        <CardDescription>GitHub-first onboarding with generated tenant IDs and installation-driven repository setup.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <WizardProgress step={step} />

        {step === 0 ? (
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

        {step === 1 ? (
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-2 md:col-span-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">MCP Endpoint</label>
              <Input
                value={values.jira.mcp_endpoint}
                onChange={(event) =>
                  setValues((prev) => ({ ...prev, jira: { ...prev.jira, mcp_endpoint: event.target.value } }))
                }
                placeholder="https://mcp.example.test"
              />
            </div>
            <div className="space-y-2 md:col-span-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Project Keys</label>
              <Input
                value={textFields.projectKeysText}
                onChange={(event) => setTextFields((prev) => ({ ...prev, projectKeysText: event.target.value }))}
                placeholder="TP, APP"
              />
            </div>
          </div>
        ) : null}

        {step === 2 ? (
          <div className="space-y-3">
            <p className="rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
              <CheckCircle2 className="mr-1 inline h-4 w-4" />
              Tenant <strong>{createdTenantId || previewTenantId(values.name)}</strong> is ready for GitHub install.
            </p>
            <div className="flex flex-wrap gap-2">
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
            {installationRepos.length > 0 ? (
              <div className="grid gap-3 md:grid-cols-[1fr_auto]">
                <select
                  className="h-10 rounded-md border border-input bg-background px-3 text-sm"
                  value={selectedRepoUrl}
                  onChange={(event) => setSelectedRepoUrl(event.target.value)}
                >
                  {installationRepos.map((repo) => (
                    <option key={repo.html_url} value={repo.html_url}>
                      {repo.full_name}
                    </option>
                  ))}
                </select>
                <Button variant="secondary" onClick={applyRepositorySelection}>
                  Use Selected Repo
                </Button>
              </div>
            ) : null}
          </div>
        ) : null}

        {step === 3 ? (
          <div className="space-y-3">
            <div className="space-y-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Allowlist (one URL per line)</label>
              <Textarea
                className="min-h-[120px]"
                value={textFields.allowlistText}
                onChange={(event) => setTextFields((prev) => ({ ...prev, allowlistText: event.target.value }))}
                placeholder="https://github.com/example/repo"
              />
            </div>
            <div className="space-y-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Project-to-Repo Mapping</label>
              <Textarea
                className="min-h-[120px]"
                value={textFields.mappingByProjectText}
                onChange={(event) => setTextFields((prev) => ({ ...prev, mappingByProjectText: event.target.value }))}
                placeholder="TP=https://github.com/example/repo"
              />
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

        {step === 4 ? (
          <div className="space-y-3 text-sm">
            <p className="rounded-md border bg-muted/30 p-3">
              <strong>Tenant:</strong> {createdTenantId || previewTenantId(values.name)} ({values.name || "-"})
            </p>
            <p className="rounded-md border bg-muted/30 p-3">
              <strong>Jira:</strong> {values.jira.mcp_endpoint || "-"} | keys: {joinCsv(splitCsv(textFields.projectKeysText)) || "-"}
            </p>
            <p className="rounded-md border bg-muted/30 p-3">
              <strong>GitHub installation:</strong> {values.github.installation_id || "not connected"}
            </p>
            <p className="rounded-md border bg-muted/30 p-3">
              <strong>Allowlist entries:</strong> {textFields.allowlistText.split("\n").filter(Boolean).length}
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
          <Button variant="outline" onClick={previousStep} disabled={step === 0}>
            <ArrowLeft className="mr-2 h-4 w-4" /> Back
          </Button>
          <p className="text-xs text-muted-foreground">{statusLine}</p>
          <Button onClick={() => void nextStep()} disabled={step >= STEPS.length - 1 || saving}>
            Next <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
