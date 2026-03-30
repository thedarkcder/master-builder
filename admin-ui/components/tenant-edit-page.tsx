"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { KeyRound, Link2 } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { ProjectsManager } from "@/components/projects-manager";
import { TenantForm } from "@/components/tenant-form";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  archiveTenant,
  disconnectJira,
  listCodexModels,
  getTenant,
  getJiraWebhookDiagnostics,
  listJiraProjects,
  listProjects,
  listGitHubRepositories,
  previewReadyGate,
  provisionJiraWebhook,
  type GitHubRepositoryRecord,
  type ReadyGatePreviewRecord,
  type JiraWebhookDiagnosticsRecord,
  startJiraConnect,
  startDiscordInstall,
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
import { canAccessPlatformAdmin, getTenantArchiveConfirmationRoute, getTenantSettingsRoute } from "@/lib/auth-routing";
import { recordToFormValues } from "@/lib/tenant-form";
import { cn } from "@/lib/utils";

type TenantEditSection =
  | "setup"
  | "integrations"
  | "jira"
  | "github"
  | "discord"
  | "health"
  | "config"
  | "projects"
  | "notifications";

export function TenantEditPage({ section }: { section: TenantEditSection }) {
  const router = useRouter();
  const params = useParams<{ tenantId: string }>();
  const searchParams = useSearchParams();
  const { credentials, principal, ready } = useAuth();
  const isPlatformAdmin = canAccessPlatformAdmin(principal);

  const [tenant, setTenant] = useState<TenantRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [readyPreview, setReadyPreview] = useState<ReadyGatePreviewRecord | null>(null);
  const [jiraWebhook, setJiraWebhook] = useState<JiraWebhookDiagnosticsRecord | null>(null);
  const [jiraWebhookBusy, setJiraWebhookBusy] = useState(false);
  const [githubRepositories, setGithubRepositories] = useState<GitHubRepositoryRecord[]>([]);
  const [codexModels, setCodexModels] = useState<{ id: string; label: string; description?: string | null }[]>([]);
  const [globalCodexModel, setGlobalCodexModel] = useState("");
  const [reasoningEfforts, setReasoningEfforts] = useState<{ id: string; label: string; description?: string | null }[]>([]);
  const [globalCodexReasoningEffort, setGlobalCodexReasoningEffort] = useState("");
  const [repositoriesLoading, setRepositoriesLoading] = useState(false);
  const [jiraProjects, setJiraProjects] = useState<JiraProjectRecord[]>([]);
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [projectsBusy, setProjectsBusy] = useState(false);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [archiveConfirmationName, setArchiveConfirmationName] = useState("");
  const [discordEnabled, setDiscordEnabled] = useState(false);
  const [discordServerId, setDiscordServerId] = useState("");
  const [discordOnboardingChannelId, setDiscordOnboardingChannelId] = useState("");
  const [discordInviteExpirySeconds, setDiscordInviteExpirySeconds] = useState("86400");
  const [discordInviteMaxUses, setDiscordInviteMaxUses] = useState("1");

  const statusClasses = useMemo(() => {
    const normalized = statusLine.toLowerCase();
    if (normalized.includes("failed") || normalized.includes("unable") || normalized.includes("error")) {
      return "border-red-300 bg-red-50 text-red-800";
    }
    return "border-muted bg-muted/30 text-muted-foreground";
  }, [statusLine]);

  async function loadJiraWebhookDiagnostics() {
    if (!credentials || !isPlatformAdmin) {
      setJiraWebhook(null);
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

      if (isPlatformAdmin) {
        try {
          const modelCatalog = await listCodexModels(credentials, { profileName: "engineering_execution" });
          setCodexModels(modelCatalog.models);
          setGlobalCodexModel(modelCatalog.default_model);
          setReasoningEfforts(modelCatalog.reasoning_efforts);
          setGlobalCodexReasoningEffort(modelCatalog.default_reasoning_effort);
        } catch {
          // Don't block tenant settings if platform model catalog is unavailable.
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

      setDiscordEnabled(Boolean(payload.discord));
      setDiscordServerId(payload.discord?.guild_id ?? "");
      setDiscordOnboardingChannelId(payload.discord?.onboarding_channel_id ?? "");
      setDiscordInviteExpirySeconds(String(payload.discord?.onboarding_invite_expires_in_seconds ?? 86400));
      setDiscordInviteMaxUses(String(payload.discord?.onboarding_invite_max_uses ?? 1));
      if (isPlatformAdmin && (section === "jira" || section === "notifications")) {
        await loadJiraWebhookDiagnostics();
      } else {
        setJiraWebhook(null);
      }
      const loadedProjects = await listProjects(credentials, params.tenantId);
      setProjects(loadedProjects);
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
  }, [ready, credentials, isPlatformAdmin, section]);

  useEffect(() => {
    if (searchParams.get("github_install") === "success") {
      setStatusLine("GitHub App install callback received. Installation details were saved.");
    }
    if (searchParams.get("discord_install") === "success") {
      setStatusLine("Discord bot install callback received. Confirm the onboarding channel and invite settings, then save.");
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
    if (tenant.is_enabled && archiveConfirmationName.trim() !== tenant.name.trim()) {
      setStatusLine(`Enter "${tenant.name}" to archive this workspace.`);
      return;
    }
    setArchiveBusy(true);
    try {
      const updated = tenant.is_enabled
        ? await archiveTenant(credentials, tenant.tenant_id)
        : await unarchiveTenant(credentials, tenant.tenant_id);
      setTenant(updated);
      setStatusLine(`Tenant ${action}d: ${updated.tenant_id}.`);
      if (!updated.is_enabled) {
        setArchiveConfirmationName("");
        router.push(
          getTenantArchiveConfirmationRoute(principal, updated.tenant_id, {
            purgeAfterAt: updated.purge_after_at,
          }),
        );
        return;
      }
      setArchiveConfirmationName("");
    } catch (error) {
      setStatusLine(`Unable to ${action} tenant: ${(error as Error).message}`);
    } finally {
      setArchiveBusy(false);
    }
  }

  async function saveDiscordSettings() {
    if (!credentials || !tenant) {
      return;
    }
    setSaving(true);
    try {
      const updated = await updateTenant(credentials, tenant.tenant_id, {
        name: tenant.name,
        is_enabled: tenant.is_enabled,
        jira: tenant.jira,
        github: tenant.github,
        repos: tenant.repos,
        policy: tenant.policy,
        discord: discordEnabled
          ? {
              ...(tenant.discord ?? {}),
              guild_id: discordServerId.trim() || null,
              onboarding_channel_id: discordOnboardingChannelId.trim() || null,
              onboarding_invite_expires_in_seconds: Number(discordInviteExpirySeconds || "0") || null,
              onboarding_invite_max_uses: Number(discordInviteMaxUses || "0") || null,
              notify_events: tenant.discord?.notify_events ?? [],
            }
          : null,
      });
      setTenant(updated);
      setDiscordEnabled(Boolean(updated.discord));
      setDiscordServerId(updated.discord?.guild_id ?? "");
      setDiscordOnboardingChannelId(updated.discord?.onboarding_channel_id ?? "");
      setDiscordInviteExpirySeconds(String(updated.discord?.onboarding_invite_expires_in_seconds ?? 86400));
      setDiscordInviteMaxUses(String(updated.discord?.onboarding_invite_max_uses ?? 1));
      setStatusLine("Discord tenant settings saved.");
    } catch (error) {
      setStatusLine(`Unable to save Discord settings: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function connectDiscordInstall(returnTo: "edit" | "wizard" = "edit") {
    if (!credentials) {
      return;
    }
    try {
      const result = await startDiscordInstall(credentials, params.tenantId, { returnTo });
      window.location.href = result.install_url;
    } catch (error) {
      setStatusLine(`Unable to start Discord bot install: ${(error as Error).message}`);
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
            <Link href="/tenants/select">Back to Tenants</Link>
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
    : !isPlatformAdmin
      ? { label: "hidden", detail: "Webhook diagnostics are available from platform status." }
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
    <div className="space-y-6">
      <div className="space-y-2">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="space-y-1">
            <h2 className="text-2xl font-semibold tracking-tight">{tenant.name}</h2>
            <p className="text-sm text-muted-foreground">Configure integrations, delivery policy, and workspace operations.</p>
          </div>
          <div className="rounded-full border px-3 py-1 text-xs font-medium uppercase tracking-[0.2em] text-muted-foreground">
            {tenant.tenant_id}
          </div>
        </div>
        {statusLine ? <p className={cn("rounded-md border px-3 py-2 text-sm", statusClasses)}>{statusLine}</p> : null}
      </div>

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
                <Link href={`/${encodeURIComponent(tenant.tenant_id)}/secrets`}>
                  <KeyRound className="mr-2 h-4 w-4" />
                  Manage Secrets
                </Link>
              </Button>
              <Button asChild variant="outline">
                <Link href={getTenantSettingsRoute(tenant.tenant_id, "integrations")}>Open Integrations</Link>
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
                <Link href={getTenantSettingsRoute(tenant.tenant_id, "jira")}>Open Jira</Link>
              </Button>
            </div>
            <div className="space-y-2 rounded-md border p-3">
              <p className="font-medium">GitHub</p>
              <p className="text-muted-foreground">Install or reconnect GitHub App for this tenant.</p>
              <Button asChild variant="outline" size="sm">
                <Link href={getTenantSettingsRoute(tenant.tenant_id, "github")}>Open GitHub</Link>
              </Button>
            </div>
            <div className="space-y-2 rounded-md border p-3">
              <p className="font-medium">Discord</p>
              <p className="text-muted-foreground">Manage notification and command settings for tenant channels.</p>
              <Button asChild variant="outline" size="sm">
                <Link href={getTenantSettingsRoute(tenant.tenant_id, "discord")}>Open Discord</Link>
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
            {isPlatformAdmin ? (
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
            ) : null}
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
              codexModels={codexModels}
              reasoningEfforts={reasoningEfforts}
              globalCodexModel={globalCodexModel}
              globalCodexReasoningEffort={globalCodexReasoningEffort}
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
            <CardDescription>Install the tenant bot, confirm the connected guild, and configure onboarding invites.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2 text-sm">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  className="h-4 w-4 rounded border-input"
                  checked={discordEnabled}
                  onChange={(event) => setDiscordEnabled(event.target.checked)}
                />
                <span>Enable Discord</span>
              </label>
              <div className="rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                Connected guild: <strong>{tenant.discord?.guild_id ?? "not installed yet"}</strong>
                <br />
                Installed at: <strong>{tenant.discord?.installed_at ?? "not installed yet"}</strong>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" onClick={() => void connectDiscordInstall("edit")}>
                  <Link2 className="mr-2 h-4 w-4" />
                  {tenant.discord?.guild_id ? "Reinstall Discord Bot" : "Install Discord Bot"}
                </Button>
              </div>
              <div className="space-y-1">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Server ID</p>
                <Input
                  value={discordServerId}
                  onChange={(event) => setDiscordServerId(event.target.value)}
                  placeholder="Discord guild/server ID"
                  disabled={!discordEnabled}
                />
              </div>
              <div className="space-y-1">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Onboarding Channel ID</p>
                <Input
                  value={discordOnboardingChannelId}
                  onChange={(event) => setDiscordOnboardingChannelId(event.target.value)}
                  placeholder="Discord channel used for join invites"
                  disabled={!discordEnabled}
                />
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                <div className="space-y-1">
                  <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Invite expiry seconds</p>
                  <Input
                    value={discordInviteExpirySeconds}
                    onChange={(event) => setDiscordInviteExpirySeconds(event.target.value)}
                    placeholder="86400"
                    disabled={!discordEnabled}
                  />
                </div>
                <div className="space-y-1">
                  <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Invite max uses</p>
                  <Input
                    value={discordInviteMaxUses}
                    onChange={(event) => setDiscordInviteMaxUses(event.target.value)}
                    placeholder="1"
                    disabled={!discordEnabled}
                  />
                </div>
              </div>
              <p className="rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                Live voice rooms are configured per project on the project Discord page. Onboarding joins use the tenant onboarding channel.
              </p>
              <Button onClick={() => void saveDiscordSettings()} disabled={saving}>
                {saving ? "Saving..." : "Save"}
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {section === "config" ? (
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Workspace configuration</CardTitle>
              <CardDescription>Update identity and policy.</CardDescription>
            </CardHeader>
            <CardContent>
              <TenantForm
                mode="edit"
                initialValues={recordToFormValues(tenant)}
                onSubmit={handleSave}
                submitting={saving}
                codexModels={codexModels}
                reasoningEfforts={reasoningEfforts}
                globalCodexModel={globalCodexModel}
                globalCodexReasoningEffort={globalCodexReasoningEffort}
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

          <Card className="border-red-200 bg-red-50/40">
            <CardHeader>
              <CardTitle>Danger zone</CardTitle>
              <CardDescription>
                Archiving disables this workspace immediately and schedules permanent deletion in 60 days.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <p className="text-sm text-muted-foreground">
                  Type <span className="font-medium text-foreground">{tenant.name}</span> to confirm.
                </p>
                <Input
                  value={archiveConfirmationName}
                  onChange={(event) => setArchiveConfirmationName(event.target.value)}
                  placeholder={tenant.name}
                  disabled={archiveBusy || !tenant.is_enabled}
                />
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <Button
                  variant="outline"
                  className={tenant.is_enabled ? "border-red-300 bg-red-600 text-white hover:bg-red-700 hover:text-white" : undefined}
                  onClick={() => void handleArchiveToggle()}
                  disabled={
                    archiveBusy ||
                    (tenant.is_enabled && archiveConfirmationName.trim() !== tenant.name.trim())
                  }
                >
                  {tenant.is_enabled ? "Archive workspace" : "Unarchive workspace"}
                </Button>
                <p className="text-xs text-muted-foreground">
                  {tenant.is_enabled
                    ? "Archived workspaces move into the archived list and can be restored before purge."
                    : `Scheduled purge: ${tenant.purge_after_at ?? "not scheduled"}`}
                </p>
              </div>
            </CardContent>
          </Card>
        </div>
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
                    <p className="font-medium">Jira Webhook Delivery</p>
                    <p className="text-xs text-muted-foreground">{jiraWebhookStatus.detail}</p>
                  </div>
                  <span className="rounded-full border px-2 py-0.5 text-xs">{jiraWebhookStatus.label}</span>
                  <Button asChild size="sm" variant="outline">
                    <Link href={getTenantSettingsRoute(tenant.tenant_id, "jira")}>Review</Link>
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
