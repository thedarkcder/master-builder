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
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  archiveTenant,
  createTenantDiscordInvite,
  createTenantInvite,
  createTenantTeam,
  disconnectJira,
  getTenantDiscordIdentity,
  listCodexModels,
  listTenantInvites,
  listTenantMembers,
  listTenantTeams,
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
  startTenantDiscordLink,
  startDiscordInstall,
  startGitHubInstall,
  resetJiraWebhook,
  resendTenantInvite,
  testGithub,
  testJira,
  unarchiveTenant,
  revokeTenantInvite,
  createProject,
  updateTenant,
  updateTenantMemberRecord,
  updateProject,
  updateTenantTeamRecord,
  type TenantRecord,
  type TenantUpdatePayload,
  type JiraProjectRecord,
  type TenantMemberRecord,
  type TenantInviteRecord,
  type TenantTeamRecord,
  type TenantDiscordIdentityRecord,
  type ProjectCreatePayload,
  type ProjectRecord,
  type ProjectUpdatePayload
} from "@/lib/api";
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
  | "access"
  | "notifications";

const TEAM_PERMISSION_OPTIONS = [
  "tenant.manage",
  "members.manage",
  "teams.manage",
  "analytics.business.view",
  "analytics.technical.view",
  "runs.business.view",
  "runs.technical.view",
  "settings.business.view",
  "settings.technical.view"
] as const;

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
  const [codexModels, setCodexModels] = useState<{ id: string; label: string; description?: string | null }[]>([]);
  const [globalCodexModel, setGlobalCodexModel] = useState("");
  const [reasoningEfforts, setReasoningEfforts] = useState<{ id: string; label: string; description?: string | null }[]>([]);
  const [globalCodexReasoningEffort, setGlobalCodexReasoningEffort] = useState("");
  const [repositoriesLoading, setRepositoriesLoading] = useState(false);
  const [jiraProjects, setJiraProjects] = useState<JiraProjectRecord[]>([]);
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [projectsBusy, setProjectsBusy] = useState(false);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [discordEnabled, setDiscordEnabled] = useState(false);
  const [discordServerId, setDiscordServerId] = useState("");
  const [discordOnboardingChannelId, setDiscordOnboardingChannelId] = useState("");
  const [discordInviteExpirySeconds, setDiscordInviteExpirySeconds] = useState("86400");
  const [discordInviteMaxUses, setDiscordInviteMaxUses] = useState("1");
  const [members, setMembers] = useState<TenantMemberRecord[]>([]);
  const [teams, setTeams] = useState<TenantTeamRecord[]>([]);
  const [invites, setInvites] = useState<TenantInviteRecord[]>([]);
  const [discordIdentity, setDiscordIdentity] = useState<TenantDiscordIdentityRecord | null>(null);
  const [accessBusy, setAccessBusy] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteName, setInviteName] = useState("");
  const [inviteRole, setInviteRole] = useState<"tenant_admin" | "technical_member" | "business_member">("business_member");
  const [inviteTeamIds, setInviteTeamIds] = useState<string>("");
  const [inviteModeOverride, setInviteModeOverride] = useState<"technical" | "non_technical" | "">("");
  const [newTeamName, setNewTeamName] = useState("");
  const [newTeamDescription, setNewTeamDescription] = useState("");
  const [newTeamPermissions, setNewTeamPermissions] = useState<string[]>(["analytics.business.view"]);

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
      const modelCatalog = await listCodexModels(credentials);
      setTenant(payload);
      setCodexModels(modelCatalog.models);
      setGlobalCodexModel(modelCatalog.default_model);
      setReasoningEfforts(modelCatalog.reasoning_efforts);
      setGlobalCodexReasoningEffort(modelCatalog.default_reasoning_effort);
      setDiscordEnabled(Boolean(payload.discord));
      setDiscordServerId(payload.discord?.guild_id ?? "");
      setDiscordOnboardingChannelId(payload.discord?.onboarding_channel_id ?? "");
      setDiscordInviteExpirySeconds(String(payload.discord?.onboarding_invite_expires_in_seconds ?? 86400));
      setDiscordInviteMaxUses(String(payload.discord?.onboarding_invite_max_uses ?? 1));
      await loadJiraWebhookDiagnostics();
      const loadedProjects = await listProjects(credentials, params.tenantId);
      setProjects(loadedProjects);
    } catch (error) {
      setStatusLine(`Failed to load tenant: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  async function loadAccessData() {
    if (!credentials) {
      return;
    }
    setAccessBusy(true);
    try {
      const [nextMembers, nextTeams, nextInvites, nextDiscordIdentity] = await Promise.all([
        listTenantMembers(credentials, params.tenantId),
        listTenantTeams(credentials, params.tenantId),
        listTenantInvites(credentials, params.tenantId),
        getTenantDiscordIdentity(credentials, params.tenantId),
      ]);
      setMembers(nextMembers);
      setTeams(nextTeams);
      setInvites(nextInvites.items);
      setDiscordIdentity(nextDiscordIdentity);
    } catch (error) {
      setStatusLine(`Failed to load access settings: ${(error as Error).message}`);
    } finally {
      setAccessBusy(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) {
      void loadTenant();
    }
  }, [ready, credentials]);

  useEffect(() => {
    if (ready && credentials && section === "access") {
      void loadAccessData();
    }
  }, [ready, credentials, section]);

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

  async function handleSaveExperienceMode(defaultMode: "technical" | "non_technical") {
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
        discord: tenant.discord,
        experience: { ...(tenant.experience ?? {}), default_mode: defaultMode },
      });
      setTenant(updated);
      setStatusLine(`Default experience set to ${defaultMode.replace("_", " ")}.`);
    } catch (error) {
      setStatusLine(`Unable to update experience mode: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function handleCreateInvite() {
    if (!credentials) {
      return;
    }
    setAccessBusy(true);
    try {
      await createTenantInvite(credentials, params.tenantId, {
        email: inviteEmail.trim(),
        full_name: inviteName.trim() || null,
        role: inviteRole,
        team_ids: inviteTeamIds
          .split(",")
          .map((value) => value.trim())
          .filter(Boolean),
        mode_override: inviteModeOverride || null,
      });
      setInviteEmail("");
      setInviteName("");
      setInviteTeamIds("");
      setInviteModeOverride("");
      await loadAccessData();
      setStatusLine("Invite created.");
    } catch (error) {
      setStatusLine(`Unable to create invite: ${(error as Error).message}`);
    } finally {
      setAccessBusy(false);
    }
  }

  async function handleCreateTeam() {
    if (!credentials) {
      return;
    }
    setAccessBusy(true);
    try {
      await createTenantTeam(credentials, params.tenantId, {
        name: newTeamName.trim(),
        description: newTeamDescription.trim() || null,
        permission_keys: newTeamPermissions,
      });
      setNewTeamName("");
      setNewTeamDescription("");
      setNewTeamPermissions(["analytics.business.view"]);
      await loadAccessData();
      setStatusLine("Team created.");
    } catch (error) {
      setStatusLine(`Unable to create team: ${(error as Error).message}`);
    } finally {
      setAccessBusy(false);
    }
  }

  async function handleToggleTeamPermission(team: TenantTeamRecord, permissionKey: string) {
    if (!credentials) {
      return;
    }
    const nextPermissions = team.permission_keys.includes(permissionKey)
      ? team.permission_keys.filter((key) => key !== permissionKey)
      : [...team.permission_keys, permissionKey];
    setAccessBusy(true);
    try {
      await updateTenantTeamRecord(credentials, params.tenantId, team.team_id, {
        name: team.name,
        description: team.description,
        permission_keys: nextPermissions,
      });
      await loadAccessData();
      setStatusLine(`Updated team ${team.name}.`);
    } catch (error) {
      setStatusLine(`Unable to update team: ${(error as Error).message}`);
    } finally {
      setAccessBusy(false);
    }
  }

  async function handleUpdateMember(
    member: TenantMemberRecord,
    patch: Partial<Pick<TenantMemberRecord, "role" | "mode_override" | "is_active" | "team_ids">>
  ) {
    if (!credentials) {
      return;
    }
    setAccessBusy(true);
    try {
      await updateTenantMemberRecord(credentials, params.tenantId, member.membership_id, {
        role: (patch.role ?? member.role) as "tenant_admin" | "technical_member" | "business_member",
        team_ids: patch.team_ids ?? member.team_ids,
        mode_override: (patch.mode_override ?? member.mode_override) as "technical" | "non_technical" | null,
        is_active: patch.is_active ?? member.is_active,
      });
      await loadAccessData();
      setStatusLine(`Updated member ${member.email}.`);
    } catch (error) {
      setStatusLine(`Unable to update member: ${(error as Error).message}`);
    } finally {
      setAccessBusy(false);
    }
  }

  async function handleInviteAction(inviteId: string, action: "resend" | "revoke") {
    if (!credentials) {
      return;
    }
    setAccessBusy(true);
    try {
      if (action === "resend") {
        await resendTenantInvite(credentials, params.tenantId, inviteId);
      } else {
        await revokeTenantInvite(credentials, params.tenantId, inviteId);
      }
      await loadAccessData();
      setStatusLine(`Invite ${action} complete.`);
    } catch (error) {
      setStatusLine(`Unable to ${action} invite: ${(error as Error).message}`);
    } finally {
      setAccessBusy(false);
    }
  }

  async function handleLinkDiscord() {
    if (!credentials) {
      return;
    }
    try {
      const result = await startTenantDiscordLink(credentials, params.tenantId, "/get-started");
      window.location.href = result.authorize_url;
    } catch (error) {
      setStatusLine(`Unable to start Discord link: ${(error as Error).message}`);
    }
  }

  async function handleCreateDiscordInvite() {
    if (!credentials) {
      return;
    }
    try {
      const invite = await createTenantDiscordInvite(credentials, params.tenantId);
      await navigator.clipboard.writeText(invite.invite_url);
      await loadAccessData();
      setStatusLine("Discord invite generated and copied to clipboard.");
    } catch (error) {
      setStatusLine(`Unable to generate Discord invite: ${(error as Error).message}`);
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

      {section === "access" ? (
        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Experience</CardTitle>
              <CardDescription>Choose the default business or technical experience for this tenant.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4 text-sm">
              <div className="flex flex-wrap items-center gap-3">
                <Button
                  variant={(tenant.experience?.default_mode ?? "technical") === "non_technical" ? "default" : "outline"}
                  onClick={() => void handleSaveExperienceMode("non_technical")}
                  disabled={saving}
                >
                  Non-technical
                </Button>
                <Button
                  variant={(tenant.experience?.default_mode ?? "technical") === "technical" ? "default" : "outline"}
                  onClick={() => void handleSaveExperienceMode("technical")}
                  disabled={saving}
                >
                  Technical
                </Button>
              </div>
              <p className="rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                Non-technical mode hides token, diagnostics, and cost-heavy surfaces and defaults analytics to delivery summaries.
              </p>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Discord Onboarding</CardTitle>
              <CardDescription>Link your Discord identity and generate onboarding invites for members.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              <p>
                <strong>Identity:</strong>{" "}
                {discordIdentity?.linked
                  ? `${discordIdentity.discord_global_name ?? discordIdentity.discord_username ?? discordIdentity.discord_user_id}`
                  : "Not linked"}
              </p>
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" onClick={() => void handleLinkDiscord()}>
                  Link Discord
                </Button>
                <Button variant="outline" onClick={() => void handleCreateDiscordInvite()}>
                  Generate Join Invite
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Members</CardTitle>
              <CardDescription>Review role, mode, onboarding, team assignments, and Discord status.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              {members.length === 0 ? (
                <p className="text-muted-foreground">No members found.</p>
              ) : (
                <div className="space-y-3">
                  {members.map((member) => (
                    <div key={member.membership_id} className="rounded-lg border p-3">
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <p className="font-medium">{member.full_name || member.email}</p>
                          <p className="text-xs text-muted-foreground">{member.email}</p>
                          <p className="text-xs text-muted-foreground">
                            Discord:{" "}
                            {member.discord_state.welcome_status
                              ? `${String(member.discord_state.linked ? "linked" : "not linked")} / ${String(
                                  member.discord_state.guild_joined ? "joined" : "not joined"
                                )} / welcome ${String(member.discord_state.welcome_status)}`
                              : String(member.discord_state.linked ? "linked" : "not linked")}
                          </p>
                        </div>
                        <div className="flex flex-wrap gap-2">
                          <select
                            className="rounded-md border bg-background px-2 py-1"
                            value={member.role}
                            onChange={(event) =>
                              void handleUpdateMember(member, {
                                role: event.target.value as TenantMemberRecord["role"],
                              })
                            }
                          >
                            <option value="business_member">business_member</option>
                            <option value="technical_member">technical_member</option>
                            <option value="tenant_admin">tenant_admin</option>
                          </select>
                          <select
                            className="rounded-md border bg-background px-2 py-1"
                            value={member.mode_override ?? ""}
                            onChange={(event) =>
                              void handleUpdateMember(member, {
                                mode_override: (event.target.value || null) as TenantMemberRecord["mode_override"],
                              })
                            }
                          >
                            <option value="">tenant default</option>
                            <option value="non_technical">non_technical</option>
                            <option value="technical">technical</option>
                          </select>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => void handleUpdateMember(member, { is_active: !member.is_active })}
                          >
                            {member.is_active ? "Deactivate" : "Reactivate"}
                          </Button>
                        </div>
                      </div>
                      <div className="mt-3 flex flex-wrap gap-2">
                        {teams.map((team) => {
                          const assigned = member.team_ids.includes(team.team_id);
                          return (
                            <Button
                              key={`${member.membership_id}-${team.team_id}`}
                              variant={assigned ? "default" : "outline"}
                              size="sm"
                              onClick={() =>
                                void handleUpdateMember(member, {
                                  team_ids: assigned
                                    ? member.team_ids.filter((teamId) => teamId !== team.team_id)
                                    : [...member.team_ids, team.team_id],
                                })
                              }
                            >
                              {team.name}
                            </Button>
                          );
                        })}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Teams</CardTitle>
              <CardDescription>Create teams and tune permission bundles.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4 text-sm">
              <div className="grid gap-3 rounded-lg border p-3 md:grid-cols-2">
                <Input placeholder="Team name" value={newTeamName} onChange={(event) => setNewTeamName(event.target.value)} />
                <Input
                  placeholder="Description"
                  value={newTeamDescription}
                  onChange={(event) => setNewTeamDescription(event.target.value)}
                />
                <div className="md:col-span-2 flex flex-wrap gap-2">
                  {TEAM_PERMISSION_OPTIONS.map((permissionKey) => {
                    const selected = newTeamPermissions.includes(permissionKey);
                    return (
                      <Button
                        key={permissionKey}
                        type="button"
                        variant={selected ? "default" : "outline"}
                        size="sm"
                        onClick={() =>
                          setNewTeamPermissions((current) =>
                            current.includes(permissionKey)
                              ? current.filter((item) => item !== permissionKey)
                              : [...current, permissionKey]
                          )
                        }
                      >
                        {permissionKey}
                      </Button>
                    );
                  })}
                </div>
                <div className="md:col-span-2">
                  <Button onClick={() => void handleCreateTeam()} disabled={accessBusy || !newTeamName.trim()}>
                    Create team
                  </Button>
                </div>
              </div>

              <div className="space-y-3">
                {teams.map((team) => (
                  <div key={team.team_id} className="rounded-lg border p-3">
                    <p className="font-medium">{team.name}</p>
                    <p className="text-xs text-muted-foreground">{team.description || "No description"}</p>
                    <div className="mt-3 flex flex-wrap gap-2">
                      {TEAM_PERMISSION_OPTIONS.map((permissionKey) => (
                        <Button
                          key={`${team.team_id}-${permissionKey}`}
                          variant={team.permission_keys.includes(permissionKey) ? "default" : "outline"}
                          size="sm"
                          onClick={() => void handleToggleTeamPermission(team, permissionKey)}
                        >
                          {permissionKey}
                        </Button>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Invites</CardTitle>
              <CardDescription>Create, resend, and revoke email invites.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4 text-sm">
              <div className="grid gap-3 rounded-lg border p-3 md:grid-cols-2">
                <Input placeholder="Email" value={inviteEmail} onChange={(event) => setInviteEmail(event.target.value)} />
                <Input placeholder="Full name" value={inviteName} onChange={(event) => setInviteName(event.target.value)} />
                <select
                  className="rounded-md border bg-background px-3 py-2"
                  value={inviteRole}
                  onChange={(event) =>
                    setInviteRole(event.target.value as "tenant_admin" | "technical_member" | "business_member")
                  }
                >
                  <option value="business_member">business_member</option>
                  <option value="technical_member">technical_member</option>
                  <option value="tenant_admin">tenant_admin</option>
                </select>
                <select
                  className="rounded-md border bg-background px-3 py-2"
                  value={inviteModeOverride}
                  onChange={(event) => setInviteModeOverride(event.target.value as "technical" | "non_technical" | "")}
                >
                  <option value="">tenant default mode</option>
                  <option value="non_technical">non_technical</option>
                  <option value="technical">technical</option>
                </select>
                <div className="md:col-span-2">
                  <Input
                    placeholder="Team IDs, comma separated"
                    value={inviteTeamIds}
                    onChange={(event) => setInviteTeamIds(event.target.value)}
                  />
                </div>
                <div className="md:col-span-2">
                  <Button onClick={() => void handleCreateInvite()} disabled={accessBusy || !inviteEmail.trim()}>
                    Send invite
                  </Button>
                </div>
              </div>

              <div className="space-y-3">
                {invites.map((invite) => (
                  <div key={invite.invite_id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3">
                    <div>
                      <p className="font-medium">{invite.email}</p>
                      <p className="text-xs text-muted-foreground">
                        {invite.status} • {invite.role} • teams {invite.team_ids.length > 0 ? invite.team_ids.join(", ") : "none"}
                      </p>
                    </div>
                    <div className="flex gap-2">
                      <Button variant="outline" size="sm" onClick={() => void handleInviteAction(invite.invite_id, "resend")}>
                        Resend
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => void handleInviteAction(invite.invite_id, "revoke")}>
                        Revoke
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        </div>
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
