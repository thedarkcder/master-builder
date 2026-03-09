"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import {
  Archive,
  ArrowLeft,
  ExternalLink,
  KeyRound,
  Plus,
  RefreshCw,
  SlidersHorizontal,
  Trash2,
} from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { ProjectKnowledgeBaseSection } from "@/components/project-knowledge-base-section";
import { ProjectNotificationsContent } from "@/components/tenant-project-discord-page";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  getProject,
  getTenant,
  listGitHubRepositories,
  listJiraProjects,
  listRuns,
  updateProject,
  type ProjectRecord,
  type RunRecord,
} from "@/lib/api";

type Tab = "overview" | "runs" | "knowledge-base" | "notifications" | "secrets";

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "runs", label: "Runs" },
  { id: "knowledge-base", label: "Knowledge Base" },
  { id: "notifications", label: "Notifications" },
  { id: "secrets", label: "Secrets" },
];

const STATUS_BORDER: Record<string, string> = {
  succeeded: "border-l-success",
  failed: "border-l-destructive",
  blocked: "border-l-warning",
  running: "border-l-info",
  pending: "border-l-muted-foreground",
};

export function TenantProjectDetailsPage() {
  const params = useParams<{ tenantId: string; projectId: string }>();
  const { credentials, ready } = useAuth();

  const [activeTab, setActiveTab] = useState<Tab>("overview");

  // Project state
  const [project, setProject] = useState<ProjectRecord | null>(null);
  const [form, setForm] = useState({ name: "", github_repository: "", jira_project_key: "" });
  const [busy, setBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [repoOptions, setRepoOptions] = useState<string[]>([]);
  const [jiraOptions, setJiraOptions] = useState<string[]>([]);

  // Runs state
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [runsBusy, setRunsBusy] = useState(false);
  const [runIssueFilter, setRunIssueFilter] = useState("");
  const [runStatusFilter, setRunStatusFilter] = useState("");
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
        status: runStatusFilter || undefined,
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
      setForm({
        name: payload.name,
        github_repository: payload.github_repository,
        jira_project_key: payload.jira_project_key,
      });
      setSecretRefs(payload.secret_refs ?? {});
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
      setForm({ name: updated.name, github_repository: updated.github_repository, jira_project_key: updated.jira_project_key });
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
        is_archived: project.is_archived,
      });
      setProject(updated);
      setForm({ name: updated.name, github_repository: updated.github_repository, jira_project_key: updated.jira_project_key });
      setStatusLine("Project details saved.");
    } catch (error) {
      setStatusLine(`Unable to update project: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function saveSecretRefs() {
    if (!credentials || !project) return;
    setSecretsBusy(true);
    try {
      const updated = await updateProject(credentials, params.tenantId, params.projectId, {
        name: project.name,
        github_repository: project.github_repository,
        jira_project_key: project.jira_project_key,
        policy_overrides: project.policy_overrides,
        environment: project.environment,
        secret_refs: secretRefs,
        discord: project.discord,
        is_archived: project.is_archived,
      });
      setProject(updated);
      setSecretRefs(updated.secret_refs ?? {});
      setSecretsStatusLine("Secret references saved.");
    } catch (error) {
      setSecretsStatusLine(`Save failed: ${(error as Error).message}`);
    } finally {
      setSecretsBusy(false);
    }
  }

  function addSecretRef() {
    const key = newSecretKey.trim();
    const ref = newSecretRef.trim();
    if (!key || !ref) {
      setSecretsStatusLine("Both variable name and secret ref path are required.");
      return;
    }
    setSecretRefs((prev) => ({ ...prev, [key]: ref }));
    setNewSecretKey("");
    setNewSecretRef("");
    setSecretsStatusLine("");
  }

  function removeSecretRef(key: string) {
    setSecretRefs((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
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
        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between gap-2">
              <CardTitle className="text-base">Project Details</CardTitle>
              <Button variant="ghost" size="sm" onClick={() => void loadOptions()} disabled={busy}>
                <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
                Refresh options
              </Button>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            {statusLine ? (
              <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p>
            ) : null}
            {project ? (
              <>
                <div className="grid gap-4 md:grid-cols-3">
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
                </div>
                <div className="flex items-center gap-2 border-t pt-4">
                  <Button size="sm" onClick={() => void saveDetails()} disabled={busy}>
                    Save changes
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => void toggleArchive()} disabled={busy}>
                    <Archive className="mr-1.5 h-3.5 w-3.5" />
                    {project.is_archived ? "Unarchive" : "Archive"}
                  </Button>
                </div>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">Loading project details…</p>
            )}
          </CardContent>
        </Card>
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
              <Input
                className="h-8 w-32 text-sm"
                value={runStatusFilter}
                onChange={(e) => setRunStatusFilter(e.target.value)}
                placeholder="Status"
                disabled={runsBusy}
              />
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
                  setRunStatusFilter("");
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

      {/* ── Knowledge Base tab ───────────────────────────────────────────── */}
      {activeTab === "knowledge-base" && project ? (
        <ProjectKnowledgeBaseSection
          credentials={credentials}
          tenantId={params.tenantId}
          projectId={params.projectId}
        />
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
                <CardTitle className="text-base">Secret References</CardTitle>
                <p className="text-sm text-muted-foreground">
                  Map variable names to platform or tenant secret refs. These are resolved at runtime and injected into
                  the project environment.
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
                  Secret ref path
                </label>
                <Input
                  className="h-8 text-sm font-mono"
                  value={newSecretRef}
                  onChange={(e) => setNewSecretRef(e.target.value)}
                  placeholder="e.g. platform/OPENAI_KEY or tenant/acme/MY_SECRET"
                  disabled={secretsBusy}
                />
              </div>
              <Button size="sm" onClick={addSecretRef} disabled={secretsBusy} className="h-8">
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
                  Add a variable name and secret ref path above to get started.
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
                            onClick={() => removeSecretRef(key)}
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
