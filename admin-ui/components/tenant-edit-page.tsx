"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { KeyRound, Link2 } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { ProjectsManager } from "@/components/projects-manager";
import { TenantForm } from "@/components/tenant-form";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  archiveTenant,
  approveDiscordAllowlistRequest,
  disconnectJira,
  getTenant,
  getJiraWebhookDiagnostics,
  listJiraProjects,
  listProjects,
  listGitHubRepositories,
  listDiscordAllowlistRequests,
  previewReadyGate,
  provisionJiraWebhook,
  type GitHubRepositoryRecord,
  type DiscordAllowlistRequestRecord,
  type ReadyGatePreviewRecord,
  type JiraWebhookDiagnosticsRecord,
  startJiraConnect,
  startGitHubInstall,
  resetJiraWebhook,
  testGithub,
  testJira,
  unarchiveTenant,
  createProject,
  updateTenant,
  updateProject,
  type TenantRecord,
  type TenantUpdatePayload,
  type JiraProjectRecord,
  type ProjectCreatePayload,
  type ProjectRecord,
  type ProjectUpdatePayload
} from "@/lib/api";
import { recordToFormValues } from "@/lib/tenant-form";
import { cn } from "@/lib/utils";

type TenantEditSection = "setup" | "integrations" | "jira" | "github" | "discord" | "health" | "config" | "projects" | "notifications";

export function TenantEditPage({ section }: { section: TenantEditSection }) {
  const params = useParams<{ tenantId: string }>();
  const searchParams = useSearchParams();
  const { credentials, ready } = useAuth();

  const [tenant, setTenant] = useState<TenantRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [readyPreview, setReadyPreview] = useState<ReadyGatePreviewRecord | null>(null);
  const [jiraWebhook, setJiraWebhook] = useState<JiraWebhookDiagnosticsRecord | null>(null);
  const [jiraWebhookBusy, setJiraWebhookBusy] = useState(false);
  const [githubRepositories, setGithubRepositories] = useState<GitHubRepositoryRecord[]>([]);
  const [repositoriesLoading, setRepositoriesLoading] = useState(false);
  const [allowlistRequests, setAllowlistRequests] = useState<DiscordAllowlistRequestRecord[]>([]);
  const [allowlistBusyUserId, setAllowlistBusyUserId] = useState<string | null>(null);
  const [jiraProjects, setJiraProjects] = useState<JiraProjectRecord[]>([]);
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [projectsBusy, setProjectsBusy] = useState(false);
  const [archiveBusy, setArchiveBusy] = useState(false);

  const statusClasses = useMemo(() => {
    const normalized = statusLine.toLowerCase();
    if (normalized.includes("failed") || normalized.includes("unable") || normalized.includes("error")) {
      return "border-red-300 bg-red-50 text-red-800";
    }
    return "border-muted bg-muted/30 text-muted-foreground";
  }, [statusLine]);

  async function loadJiraWebhookDiagnostics() {
    if (!credentials) {
      return;
    }
    try {
      const diagnostics = await getJiraWebhookDiagnostics(credentials, params.tenantId);
      setJiraWebhook(diagnostics);
    } catch (error) {
      setStatusLine(`Failed to load Jira webhook diagnostics: ${(error as Error).message}`);
    }
  }

  async function loadGitHubRepositories({ silent = false }: { silent?: boolean } = {}) {
    if (!credentials) {
      return;
    }
    setRepositoriesLoading(true);
    try {
      const repos = await listGitHubRepositories(credentials, params.tenantId);
      setGithubRepositories(repos);
      if (!silent) {
        setStatusLine(`Loaded ${repos.length} repository option(s) from GitHub installation.`);
      }
    } catch {
      setGithubRepositories([]);
      if (!silent) {
        setStatusLine("Unable to load repositories from GitHub installation.");
      }
    } finally {
      setRepositoriesLoading(false);
    }
  }

  async function loadTenant() {
    if (!credentials) {
      return;
    }
    setLoading(true);
    try {
      const payload = await getTenant(credentials, params.tenantId);
      setTenant(payload);
      await loadJiraWebhookDiagnostics();
      await loadGitHubRepositories({ silent: true });
      if (payload.jira.connection_id) {
        try {
          const availableProjects = await listJiraProjects(credentials, payload.jira.connection_id);
          setJiraProjects(availableProjects);
        } catch {
          setJiraProjects([]);
        }
      } else {
        setJiraProjects([]);
      }
      const loadedProjects = await listProjects(credentials, params.tenantId);
      setProjects(loadedProjects);
      const requests = await listDiscordAllowlistRequests(credentials, params.tenantId);
      setAllowlistRequests(requests);
    } catch (error) {
      setStatusLine(`Failed to load tenant: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) {
      void loadTenant();
    }
  }, [ready, credentials]);

  useEffect(() => {
    if (searchParams.get("github_install") === "success") {
      setStatusLine("GitHub App install callback received. Installation details were saved.");
    }
    if (searchParams.get("jira_oauth") === "success") {
      setStatusLine("Jira OAuth callback received. Update project keys if needed, then save.");
    }
  }, [searchParams]);

  async function handleSave(payload: TenantUpdatePayload): Promise<void> {
    if (!credentials) {
      return;
    }
    setSaving(true);
    try {
      const updated = await updateTenant(credentials, params.tenantId, payload);
      setTenant(updated);
      setStatusLine(`Saved ${updated.tenant_id}.`);
    } catch (error) {
      setStatusLine(`Save failed: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function runHealthChecks() {
    if (!credentials) {
      return;
    }
    try {
      const jira = await testJira(credentials, params.tenantId);
      const github = await testGithub(credentials, params.tenantId);
      setStatusLine(
        `${params.tenantId}: Jira ${jira.ok ? "ok" : "fail"} (${jira.details}); GitHub ${github.ok ? "ok" : "fail"} (${github.details})`
      );
    } catch (error) {
      setStatusLine(`Health check failed: ${(error as Error).message}`);
    }
  }

  async function handleArchiveToggle() {
    if (!credentials || !tenant) {
      return;
    }
    const action = tenant.is_enabled ? "archive" : "unarchive";
    const confirmed = window.confirm(
      tenant.is_enabled
        ? `Archive tenant '${tenant.tenant_id}'? This disables run intake and webhook processing.`
        : `Unarchive tenant '${tenant.tenant_id}'?`
    );
    if (!confirmed) {
      return;
    }
    setArchiveBusy(true);
    try {
      const updated = tenant.is_enabled
        ? await archiveTenant(credentials, tenant.tenant_id)
        : await unarchiveTenant(credentials, tenant.tenant_id);
      setTenant(updated);
      setStatusLine(`Tenant ${action}d: ${updated.tenant_id}.`);
    } catch (error) {
      setStatusLine(`Unable to ${action} tenant: ${(error as Error).message}`);
    } finally {
      setArchiveBusy(false);
    }
  }

  async function runReadyPreview() {
    if (!credentials) {
      return;
    }
    try {
      const preview = await previewReadyGate(credentials, params.tenantId, 10);
      setReadyPreview(preview);
      setStatusLine(`Ready preview loaded: ${preview.eligible_issues.length} issue(s) currently eligible.`);
    } catch (error) {
      setStatusLine(`Ready preview failed: ${(error as Error).message}`);
    }
  }

  async function connectGitHubApp() {
    if (!credentials) {
      return;
    }
    try {
      const result = await startGitHubInstall(credentials, params.tenantId, { returnTo: "edit" });
      window.location.href = result.install_url;
    } catch (error) {
      setStatusLine(`Unable to start GitHub App install: ${(error as Error).message}`);
    }
  }

  async function connectJira() {
    if (!credentials) {
      return;
    }
    try {
      const result = await startJiraConnect(credentials, { returnTo: "edit", tenantId: params.tenantId });
      window.location.href = result.authorize_url;
    } catch (error) {
      setStatusLine(`Unable to start Jira OAuth: ${(error as Error).message}`);
    }
  }

  async function handleProvisionJiraWebhook() {
    if (!credentials) {
      return;
    }
    setJiraWebhookBusy(true);
    try {
      const result = await provisionJiraWebhook(credentials, params.tenantId);
      setStatusLine(result.details);
      await loadJiraWebhookDiagnostics();
      await loadTenant();
    } catch (error) {
      setStatusLine(`Unable to provision Jira webhook: ${(error as Error).message}`);
    } finally {
      setJiraWebhookBusy(false);
    }
  }

  async function handleResetJiraWebhook() {
    if (!credentials) {
      return;
    }
    setJiraWebhookBusy(true);
    try {
      const result = await resetJiraWebhook(credentials, params.tenantId);
      setStatusLine(result.details);
      await loadJiraWebhookDiagnostics();
      await loadTenant();
    } catch (error) {
      setStatusLine(`Unable to reset Jira webhook: ${(error as Error).message}`);
    } finally {
      setJiraWebhookBusy(false);
    }
  }

  async function handleDisconnectJira() {
    if (!credentials) {
      return;
    }
    setJiraWebhookBusy(true);
    try {
      const result = await disconnectJira(credentials, params.tenantId);
      setStatusLine(result.details);
      await loadJiraWebhookDiagnostics();
      await loadTenant();
    } catch (error) {
      setStatusLine(`Unable to disconnect Jira: ${(error as Error).message}`);
    } finally {
      setJiraWebhookBusy(false);
    }
  }

  async function handleApproveAllowlistRequest(userId: string) {
    if (!credentials) {
      return;
    }
    setAllowlistBusyUserId(userId);
    try {
      const result = await approveDiscordAllowlistRequest(credentials, params.tenantId, userId);
      setStatusLine(result.details);
      await loadTenant();
    } catch (error) {
      setStatusLine(`Unable to approve allowlist request: ${(error as Error).message}`);
    } finally {
      setAllowlistBusyUserId(null);
    }
  }

  async function refreshProjectSources() {
    if (!credentials) {
      return;
    }
    setProjectsBusy(true);
    try {
      await loadGitHubRepositories({ silent: true });
      if (tenant?.jira.connection_id) {
        const availableProjects = await listJiraProjects(credentials, tenant.jira.connection_id);
        setJiraProjects(availableProjects);
      }
      setStatusLine("Project option sources refreshed.");
    } catch (error) {
      setStatusLine(`Unable to refresh project options: ${(error as Error).message}`);
    } finally {
      setProjectsBusy(false);
    }
  }

  async function handleCreateProject(payload: ProjectCreatePayload) {
    if (!credentials) {
      return;
    }
    setProjectsBusy(true);
    try {
      await createProject(credentials, params.tenantId, payload);
      const refreshed = await listProjects(credentials, params.tenantId);
      setProjects(refreshed);
      setStatusLine("Project created.");
    } catch (error) {
      setStatusLine(`Unable to create project: ${(error as Error).message}`);
    } finally {
      setProjectsBusy(false);
    }
  }

  async function handleUpdateProject(projectId: string, payload: ProjectUpdatePayload) {
    if (!credentials) {
      return;
    }
    setProjectsBusy(true);
    try {
      await updateProject(credentials, params.tenantId, projectId, payload);
      const refreshed = await listProjects(credentials, params.tenantId);
      setProjects(refreshed);
      setStatusLine(payload.is_archived ? "Project archived." : "Project updated.");
    } catch (error) {
      setStatusLine(`Unable to update project: ${(error as Error).message}`);
    } finally {
      setProjectsBusy(false);
    }
  }

  if (loading) {
    return (
      <Card>
        <CardHeader>
          <Skeleton className="h-6 w-64" />
          <Skeleton className="h-4 w-80" />
        </CardHeader>
        <CardContent className="space-y-3">
          <Skeleton className="h-10 w-full" />
          <div className="grid gap-4 border-t pt-3 md:grid-cols-[220px_1fr]">
            <div className="space-y-2 rounded-md border bg-muted/20 p-2">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
            <Skeleton className="h-32 w-full rounded-md border" />
          </div>
        </CardContent>
      </Card>
    );
  }

  if (!tenant) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Tenant Not Found</CardTitle>
          <CardDescription>The requested tenant could not be loaded.</CardDescription>
        </CardHeader>
        <CardContent>
          <Button asChild>
            <Link href="/tenants">Back to Tenants</Link>
          </Button>
        </CardContent>
      </Card>
    );
  }

  const githubInstalled = Boolean(tenant.github.installation_id && tenant.github.installation_id.trim());
  const githubButtonLabel = githubInstalled ? "Reconnect GitHub App" : "Install GitHub App";
  const jiraConnected = Boolean(tenant.jira.connection_id && tenant.jira.connection_id.trim());
  const jiraWebhookStatus = !jiraConnected
    ? { label: "not connected", detail: "Jira is not connected for this tenant." }
    : jiraWebhook?.last_error
      ? { label: "error", detail: jiraWebhook.last_error }
      : jiraWebhook?.recent_delivery_ok
        ? {
            label: "ok",
            detail: `Recent webhook delivery confirmed within ${jiraWebhook.recent_delivery_window_minutes} minutes.`
          }
        : {
            label: "warning",
            detail: jiraWebhook
              ? `No webhook delivery within ${jiraWebhook.recent_delivery_window_minutes} minutes.`
              : "Webhook diagnostics are loading."
          };

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between gap-3">
            <CardTitle>Edit Tenant: {tenant.tenant_id}</CardTitle>
            <Button
              variant={tenant.is_enabled ? "secondary" : "outline"}
              size="sm"
              onClick={() => void handleArchiveToggle()}
              disabled={archiveBusy}
            >
              {tenant.is_enabled ? "Archive Tenant" : "Unarchive Tenant"}
            </Button>
          </div>
          <CardDescription>Configure integrations, policies, and webhook operations.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {statusLine ? <p className={cn("rounded-md border px-3 py-2 text-sm", statusClasses)}>{statusLine}</p> : null}
        </CardContent>
      </Card>

      {section === "setup" ? (
        <Card>
          <CardHeader>
            <CardTitle>Setup Flow</CardTitle>
            <CardDescription>Follow this order to keep setup predictable and complete.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <ol className="list-decimal space-y-2 pl-5">
              <li>Manage required integration secrets.</li>
              <li>Connect integrations from the dedicated Jira and GitHub pages.</li>
              <li>Create project mappings (repo + Jira key) in Projects.</li>
              <li>Run health checks and preview ready gate.</li>
            </ol>
            <div className="flex flex-wrap gap-2 border-t pt-3">
              <Button asChild variant="outline">
                <Link href={`/tenants/${encodeURIComponent(tenant.tenant_id)}/secrets`}>
                  <KeyRound className="mr-2 h-4 w-4" />
                  Manage Secrets
                </Link>
              </Button>
              <Button asChild variant="outline">
                <Link href={`/tenants/${encodeURIComponent(tenant.tenant_id)}/edit/integrations`}>Open Integrations</Link>
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {section === "integrations" ? (
        <Card>
          <CardHeader>
            <CardTitle>Integrations</CardTitle>
            <CardDescription>Connect external systems before configuring tenant policy.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div className="space-y-2 rounded-md border p-3">
              <p className="font-medium">Jira</p>
              <p className="text-muted-foreground">Connect Jira OAuth and verify tenant board access.</p>
              <Button asChild variant="outline" size="sm">
                <Link href={`/tenants/${encodeURIComponent(tenant.tenant_id)}/edit/jira`}>Open Jira</Link>
              </Button>
            </div>
            <div className="space-y-2 rounded-md border p-3">
              <p className="font-medium">GitHub</p>
              <p className="text-muted-foreground">Install or reconnect GitHub App for this tenant.</p>
              <Button asChild variant="outline" size="sm">
                <Link href={`/tenants/${encodeURIComponent(tenant.tenant_id)}/edit/github`}>Open GitHub</Link>
              </Button>
            </div>
            <div className="space-y-2 rounded-md border p-3">
              <p className="font-medium">Discord</p>
              <p className="text-muted-foreground">Manage notification and command settings for tenant channels.</p>
              <Button asChild variant="outline" size="sm">
                <Link href={`/tenants/${encodeURIComponent(tenant.tenant_id)}/edit/discord`}>Open Discord</Link>
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {section === "jira" ? (
        <Card>
          <CardHeader>
            <CardTitle>Jira Integration</CardTitle>
            <CardDescription>Connect and manage Jira access and webhook lifecycle for this tenant.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <p>
              <strong>Status:</strong> {jiraConnected ? "Connected" : "Not connected"}
            </p>
            <p>
              <strong>Connection ID:</strong> {tenant.jira.connection_id || "-"}
            </p>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" onClick={() => void connectJira()}>
                <Link2 className="mr-2 h-4 w-4" />
                {jiraConnected ? "Reconnect Jira" : "Connect Jira"}
              </Button>
              <Button variant="outline" disabled={jiraWebhookBusy} onClick={() => void handleDisconnectJira()}>
                Disconnect Jira
              </Button>
            </div>
            <div className="space-y-2 border-t pt-3">
              <p className="font-medium">Webhook Lifecycle</p>
              <p>
                <strong>Webhook URL:</strong> {jiraWebhook?.webhook_url ?? "Loading..."}
              </p>
              <p>
                <strong>Managed webhook IDs:</strong>{" "}
                {jiraWebhook?.managed_webhook_ids.length ? jiraWebhook.managed_webhook_ids.join(", ") : "-"}
              </p>
              <p>
                <strong>Last received:</strong> {jiraWebhook?.last_received_at ?? "-"}
              </p>
              <p>
                <strong>Last issue key:</strong> {jiraWebhook?.last_issue_key ?? "-"}
              </p>
              <p>
                <strong>Recent delivery:</strong>{" "}
                {jiraWebhook
                  ? jiraWebhook.recent_delivery_ok
                    ? `ok (within ${jiraWebhook.recent_delivery_window_minutes}m)`
                    : `none within ${jiraWebhook.recent_delivery_window_minutes}m`
                  : "-"}
              </p>
              <p>
                <strong>Last error:</strong> {jiraWebhook?.last_error ?? "-"}
              </p>
              <div className="flex flex-wrap gap-2 pt-1">
                <Button variant="secondary" disabled={jiraWebhookBusy} onClick={() => void handleProvisionJiraWebhook()}>
                  Provision Webhook
                </Button>
                <Button variant="secondary" disabled={jiraWebhookBusy} onClick={() => void handleResetJiraWebhook()}>
                  Reset Webhook
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {section === "github" ? (
        <Card>
          <CardHeader>
            <CardTitle>GitHub Integration</CardTitle>
            <CardDescription>Connect GitHub App once for this tenant. Project mappings are managed in Projects.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4 text-sm">
            <p>
              <strong>Status:</strong> {githubInstalled ? "Connected" : "Not connected"}
            </p>
            <p>
              <strong>Installation ID:</strong> {tenant.github.installation_id || "-"}
            </p>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" onClick={() => void connectGitHubApp()}>
                <Link2 className="mr-2 h-4 w-4" />
                {githubButtonLabel}
              </Button>
            </div>
            <TenantForm
              mode="edit"
              initialValues={recordToFormValues(tenant)}
              onSubmit={handleSave}
              submitting={saving}
              visibleSections={{
                identity: false,
                jira: false,
                github: true,
                repository: false,
                policy: false,
                discord: false
              }}
            />
          </CardContent>
        </Card>
      ) : null}

      {section === "projects" ? (
        <Card>
          <CardHeader>
            <CardTitle>Projects</CardTitle>
            <CardDescription>Create, edit, and archive tenant projects with repo/Jira mappings.</CardDescription>
          </CardHeader>
          <CardContent>
            <ProjectsManager
              projects={projects}
              repositories={githubRepositories}
              jiraProjects={jiraProjects}
              busy={projectsBusy || repositoriesLoading}
              onRefreshOptions={() => void refreshProjectSources()}
              onCreateProject={handleCreateProject}
              onUpdateProject={handleUpdateProject}
            />
          </CardContent>
        </Card>
      ) : null}

      {section === "discord" ? (
        <Card>
          <CardHeader>
            <CardTitle>Discord Integration</CardTitle>
            <CardDescription>Configure tenant Discord notifications and command permissions.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <TenantForm
              mode="edit"
              initialValues={recordToFormValues(tenant)}
              onSubmit={handleSave}
              submitting={saving}
              visibleSections={{
                identity: false,
                jira: false,
                github: false,
                repository: false,
                policy: false,
                discord: true
              }}
            />
            <div id="discord-access-requests" className="space-y-2 rounded-md border p-3 text-sm">
              <p className="font-medium">Discord Access Requests</p>
              <p className="text-muted-foreground">Approve pending `/request` submissions from Discord users for this tenant.</p>
              {allowlistRequests.length === 0 ? (
                <p className="text-muted-foreground">No pending requests.</p>
              ) : (
                <ul className="space-y-2">
                  {allowlistRequests.map((request) => (
                    <li key={request.user_id} className="rounded-md border p-3">
                      <p>
                        <strong>User:</strong> {request.user_id}
                      </p>
                      <p>
                        <strong>Requested at:</strong> {request.requested_at}
                      </p>
                      <p>
                        <strong>Reason:</strong> {request.reason ?? "-"}
                      </p>
                      <p>
                        <strong>Channel:</strong> {request.channel_id ?? "-"}
                      </p>
                      <div className="mt-2">
                        <Button
                          variant="secondary"
                          size="sm"
                          disabled={allowlistBusyUserId === request.user_id}
                          onClick={() => void handleApproveAllowlistRequest(request.user_id)}
                        >
                          {allowlistBusyUserId === request.user_id ? "Approving..." : "Approve"}
                        </Button>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </CardContent>
        </Card>
      ) : null}

      {section === "config" ? (
        <Card>
          <CardHeader>
            <CardTitle>Tenant Configuration</CardTitle>
            <CardDescription>Update identity and policy.</CardDescription>
          </CardHeader>
          <CardContent>
            <TenantForm
              mode="edit"
              initialValues={recordToFormValues(tenant)}
              onSubmit={handleSave}
              submitting={saving}
              visibleSections={{
                identity: true,
                jira: false,
                github: false,
                repository: false,
                policy: true,
                discord: false
              }}
            />
          </CardContent>
        </Card>
      ) : null}

      {section === "health" ? (
        <Card>
          <CardHeader>
            <CardTitle>Integration Health</CardTitle>
            <CardDescription>Run checks and preview ready-gate eligibility for this tenant.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" onClick={() => void runHealthChecks()}>
                Run Health Checks
              </Button>
              <Button variant="secondary" onClick={() => void runReadyPreview()}>
                Preview Ready Gate
              </Button>
            </div>
            <div className="rounded-md border p-3">
              <p className="font-medium">Ready Gate Preview</p>
              <p className="text-muted-foreground">
                {readyPreview ? readyPreview.guidance : "Run preview to inspect currently eligible issues."}
              </p>
              {readyPreview ? (
                <div className="mt-3 space-y-2">
                  <p>
                    <strong>Ready statuses:</strong> {readyPreview.ready_statuses.join(", ")}
                  </p>
                  <p>
                    <strong>JQL:</strong> {readyPreview.ready_jql}
                  </p>
                  <div className="space-y-2">
                    <strong>Eligible issues</strong>
                    {readyPreview.eligible_issues.length ? (
                      <ul className="list-disc space-y-1 pl-5">
                        {readyPreview.eligible_issues.map((issue) => (
                          <li key={issue.key}>
                            {issue.key} - {issue.summary} ({issue.status})
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="text-muted-foreground">No currently eligible issues for this tenant.</p>
                    )}
                  </div>
                </div>
              ) : null}
            </div>
          </CardContent>
        </Card>
      ) : null}

      {section === "notifications" ? (
        <Card>
          <CardHeader>
            <CardTitle>Notifications</CardTitle>
            <CardDescription>Review important tenant events and required actions in one place.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div className="rounded-md border">
              <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-3 border-b px-3 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                <span>Notification</span>
                <span>Status</span>
                <span>Action</span>
              </div>
              <ul className="divide-y">
                <li className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-3 px-3 py-3">
                  <div className="space-y-0.5">
                    <p className="font-medium">Discord Access Requests</p>
                    <p className="text-xs text-muted-foreground">
                      Requests from Discord users asking to join the tenant allowlist.
                    </p>
                  </div>
                  <span className="rounded-full border px-2 py-0.5 text-xs">
                    {allowlistRequests.length} pending
                  </span>
                  <Button asChild size="sm" variant="outline">
                    <Link href={`/tenants/${encodeURIComponent(tenant.tenant_id)}/edit/discord#discord-access-requests`}>
                      Review
                    </Link>
                  </Button>
                </li>
                <li className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-3 px-3 py-3">
                  <div className="space-y-0.5">
                    <p className="font-medium">Jira Webhook Delivery</p>
                    <p className="text-xs text-muted-foreground">{jiraWebhookStatus.detail}</p>
                  </div>
                  <span className="rounded-full border px-2 py-0.5 text-xs">{jiraWebhookStatus.label}</span>
                  <Button asChild size="sm" variant="outline">
                    <Link href={`/tenants/${encodeURIComponent(tenant.tenant_id)}/edit/jira`}>Review</Link>
                  </Button>
                </li>
              </ul>
            </div>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
