"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft, MessageSquare } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { getProject, getTenant, listGitHubRepositories, listJiraProjects, listRuns, updateProject, type ProjectRecord, type RunRecord } from "@/lib/api";

export function TenantProjectDetailsPage() {
  const params = useParams<{ tenantId: string; projectId: string }>();
  const { credentials, ready } = useAuth();

  const [project, setProject] = useState<ProjectRecord | null>(null);
  const [form, setForm] = useState({ name: "", github_repository: "", jira_project_key: "" });
  const [busy, setBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [repoOptions, setRepoOptions] = useState<string[]>([]);
  const [jiraOptions, setJiraOptions] = useState<string[]>([]);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [runsBusy, setRunsBusy] = useState(false);

  async function loadOptions() {
    if (!credentials) {
      return;
    }
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
        setJiraOptions(jiraProjects.map((project) => project.key));
      } else {
        setJiraOptions([]);
      }
    } catch {
      setRepoOptions([]);
      setJiraOptions([]);
    }
  }

  async function loadProject() {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      const payload = await getProject(credentials, params.tenantId, params.projectId);
      setProject(payload);
      setForm({
        name: payload.name,
        github_repository: payload.github_repository,
        jira_project_key: payload.jira_project_key,
      });
      await loadRuns();
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to load project: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function loadRuns() {
    if (!credentials) {
      return;
    }
    setRunsBusy(true);
    try {
      const payload = await listRuns(credentials, {
        tenantId: params.tenantId,
        projectId: params.projectId,
      });
      setRuns(payload);
    } finally {
      setRunsBusy(false);
    }
  }

  function statusBadge(status: string) {
    if (status === "succeeded") {
      return <Badge>{status}</Badge>;
    }
    if (status === "failed" || status === "blocked") {
      return <Badge variant="secondary">{status}</Badge>;
    }
    return <Badge variant="outline">{status}</Badge>;
  }

  useEffect(() => {
    if (ready && credentials) {
      void loadProject();
    }
  }, [ready, credentials, params.tenantId, params.projectId]);

  async function toggleArchive() {
    if (!credentials || !project) {
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
        secret_refs: project.secret_refs,
        discord: project.discord,
        is_archived: !project.is_archived,
      });
      setProject(updated);
      setForm({
        name: updated.name,
        github_repository: updated.github_repository,
        jira_project_key: updated.jira_project_key,
      });
      setStatusLine(updated.is_archived ? "Project archived." : "Project unarchived.");
    } catch (error) {
      setStatusLine(`Unable to update project: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function saveDetails() {
    if (!credentials || !project) {
      return;
    }
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
        secret_refs: project.secret_refs,
        discord: project.discord,
        is_archived: project.is_archived,
      });
      setProject(updated);
      setForm({
        name: updated.name,
        github_repository: updated.github_repository,
        jira_project_key: updated.jira_project_key,
      });
      setStatusLine("Project details saved.");
    } catch (error) {
      setStatusLine(`Unable to update project: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle>{project?.name ?? params.projectId}</CardTitle>
            <CardDescription>Project details</CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" onClick={() => void loadOptions()} disabled={busy}>
              Refresh options
            </Button>
            <Button variant="outline" onClick={() => void loadRuns()} disabled={busy || runsBusy}>
              {runsBusy ? "Refreshing runs..." : "Refresh runs"}
            </Button>
            <Button asChild variant="outline">
              <Link href={`/tenants/${encodeURIComponent(params.tenantId)}/projects`}>
                <ArrowLeft className="mr-2 h-4 w-4" />
                Back to Projects
              </Link>
            </Button>
            <Button asChild variant="outline">
              <Link href={`/tenants/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(params.projectId)}/discord`}>
                <MessageSquare className="mr-2 h-4 w-4" />
                Discord
              </Link>
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {statusLine ? <p className="rounded-md border px-3 py-2 text-muted-foreground">{statusLine}</p> : null}
        {project ? (
          <>
            <div className="grid gap-3 md:grid-cols-3">
              <div className="space-y-1">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Project name</p>
                <Input
                  value={form.name}
                  onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
                  disabled={busy}
                />
              </div>
              <div className="space-y-1">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Repository</p>
                <Input
                  list="project-details-repo-options"
                  value={form.github_repository}
                  onChange={(event) => setForm((prev) => ({ ...prev, github_repository: event.target.value }))}
                  placeholder="Choose repository"
                  disabled={busy}
                />
                <datalist id="project-details-repo-options">
                  {repoOptions.map((repo) => (
                    <option key={repo} value={repo} />
                  ))}
                  {!repoOptions.includes(form.github_repository) && form.github_repository ? (
                    <option value={form.github_repository} />
                  ) : null}
                </datalist>
              </div>
              <div className="space-y-1">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Jira project</p>
                <Input
                  list="project-details-jira-options"
                  value={form.jira_project_key}
                  onChange={(event) => setForm((prev) => ({ ...prev, jira_project_key: event.target.value.toUpperCase() }))}
                  placeholder="Choose Jira project"
                  disabled={busy}
                />
                <datalist id="project-details-jira-options">
                  {jiraOptions.map((key) => (
                    <option key={key} value={key} />
                  ))}
                  {!jiraOptions.includes(form.jira_project_key) && form.jira_project_key ? (
                    <option value={form.jira_project_key} />
                  ) : null}
                </datalist>
              </div>
            </div>
            <p>
              <strong>Status:</strong> {project.is_archived ? "Archived" : "Active"}
            </p>
            <div className="flex flex-wrap gap-2 pt-2">
              <Button onClick={() => void saveDetails()} disabled={busy}>
                Save
              </Button>
              <Button variant="outline" onClick={() => void toggleArchive()} disabled={busy}>
                {project.is_archived ? "Unarchive" : "Archive"}
              </Button>
            </div>
            <div className="space-y-2 border-t pt-4">
              <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Project runs</p>
              {runs.length === 0 ? (
                <p className="rounded-md border p-3 text-sm text-muted-foreground">No runs for this project yet.</p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Run</TableHead>
                      <TableHead>Issue</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Created</TableHead>
                      <TableHead>PR</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {runs.map((run) => (
                      <TableRow key={run.run_id}>
                        <TableCell className="font-medium">
                          <Link className="text-primary hover:underline" href={`/runs/${encodeURIComponent(run.run_id)}`}>
                            {run.run_id}
                          </Link>
                        </TableCell>
                        <TableCell>{run.issue_key}</TableCell>
                        <TableCell>{statusBadge(run.status)}</TableCell>
                        <TableCell>{new Date(run.created_at).toLocaleString()}</TableCell>
                        <TableCell>
                          {run.pr_url ? (
                            <Link className="text-sm text-primary hover:underline" href={run.pr_url} target="_blank">
                              Open PR
                            </Link>
                          ) : (
                            <span className="text-xs text-muted-foreground">None</span>
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </div>
          </>
        ) : (
          <p className="text-muted-foreground">Loading project details...</p>
        )}
      </CardContent>
    </Card>
  );
}
