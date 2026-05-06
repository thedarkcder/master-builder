"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { KeyRound, Link2 } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { ProjectsManager } from "@/components/projects-manager";
import { TenantForm } from "@/components/tenant-form";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/components/ui/toast-provider";
import {
  type AdminNotificationRecord,
  archiveTenant,
  disconnectAtlassian,
  listCodexModels,
  getTenant,
  getJiraWebhookDiagnostics,
  listJiraProjects,
  listTenantNotifications,
  listProjects,
  listGitHubRepositories,
  previewReadyGate,
  provisionJiraWebhook,
  type GitHubRepositoryRecord,
  type ReadyGatePreviewRecord,
  type JiraWebhookDiagnosticsRecord,
  startAtlassianConnect,
  startDiscordInstall,
  startGitHubInstall,
  resetJiraWebhook,
  testGithub,
  testAtlassian,
  unarchiveTenant,
  createProject,
  updateTenantConfiguration,
  updateTenantDiscord,
  updateTenantGithub,
  updateTenantObservability,
  updateTenantPolicy,
  updateProjectArchiveState,
  updateProjectConfiguration,
  type TenantRecord,
  type JiraProjectRecord,
  type ProjectCreatePayload,
  type ProjectArchiveUpdatePayload,
  type ProjectConfigurationUpdatePayload,
  type ProjectRecord,
} from "@/lib/api";
import { canAccessPlatformAdmin, getTenantArchiveConfirmationRoute, getTenantSettingsRoute } from "@/lib/auth-routing";
import { formatTimestamp } from "@/lib/datetime";
import { recordToFormValues, type TenantFormPayload } from "@/lib/tenant-form";
import { cn } from "@/lib/utils";

type TenantEditSection =
  | "setup"
  | "atlassian"
  | "github"
  | "discord"
  | "health"
  | "config"
  | "observability"
  | "projects"
  | "notifications"
  | "danger";

const AUDIT_RETENTION_OPTIONS = [
  { value: 30, label: "30 days" },
  { value: 90, label: "90 days" },
  { value: 180, label: "180 days" },
  { value: 365, label: "1 year" },
  { value: 730, label: "2 years" },
  { value: 2555, label: "7 years" },
] as const;

export function TenantEditPage({ section }: { section: TenantEditSection }) {
  const router = useRouter();
  const params = useParams<{ tenantId: string }>();
  const searchParams = useSearchParams();
  const { credentials, principal, ready } = useAuth();
  const { showToast } = useToast();
  const isPlatformAdmin = canAccessPlatformAdmin(principal);

  const [tenant, setTenant] = useState<TenantRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [readyPreview, setReadyPreview] = useState<ReadyGatePreviewRecord | null>(null);
  const [jiraWebhook, setJiraWebhook] = useState<JiraWebhookDiagnosticsRecord | null>(null);
  const [notifications, setNotifications] = useState<AdminNotificationRecord[]>([]);
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
  const [showArchiveConfirm, setShowArchiveConfirm] = useState(false);
  const [discordEnabled, setDiscordEnabled] = useState(false);
  const [discordServerId, setDiscordServerId] = useState("");
  const [discordOnboardingChannelId, setDiscordOnboardingChannelId] = useState("");
  const [discordInviteExpirySeconds, setDiscordInviteExpirySeconds] = useState("86400");
  const [discordInviteMaxUses, setDiscordInviteMaxUses] = useState("1");
  const [auditRetentionDays, setAuditRetentionDays] = useState("365");
  const [auditExportEnabled, setAuditExportEnabled] = useState(true);
  const [legalHoldEnabled, setLegalHoldEnabled] = useState(false);
  const [legalHoldReason, setLegalHoldReason] = useState("");

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

  async function loadNotifications() {
    if (!credentials) {
      setNotifications([]);
      return;
    }
    try {
      const payload = await listTenantNotifications(credentials, params.tenantId);
      setNotifications(payload.notifications);
    } catch (error) {
      setStatusLine(`Failed to load notifications: ${(error as Error).message}`);
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
      setAuditRetentionDays(String(payload.policy.observability?.audit_retention_days ?? 365));
      setAuditExportEnabled(payload.policy.observability?.audit_export_enabled ?? true);
      setLegalHoldEnabled(payload.policy.observability?.legal_hold_enabled ?? false);
      setLegalHoldReason(payload.policy.observability?.legal_hold_reason ?? "");
      if (isPlatformAdmin && (section === "atlassian" || section === "notifications")) {
        await Promise.all([loadJiraWebhookDiagnostics(), loadNotifications()]);
      } else {
        setJiraWebhook(null);
        setNotifications([]);
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
    if (searchParams.get("atlassian_oauth") === "success") {
      setStatusLine("Atlassian callback received. Update project keys if needed, then save.");
    }
  }, [searchParams]);

  async function handleSave(payload: TenantFormPayload): Promise<void> {
    if (!credentials || !tenant) {
      return;
    }
    setSaving(true);
    try {
      let updated: TenantRecord;
      if (section === "github") {
        updated = await updateTenantGithub(credentials, params.tenantId, { github: payload.github });
      } else {
        updated = await updateTenantConfiguration(credentials, params.tenantId, { name: payload.name });
        updated = await updateTenantPolicy(credentials, params.tenantId, { policy: payload.policy });
      }
      setTenant(updated);
      showToast({ title: "Tenant saved", description: updated.tenant_id, tone: "success" });
    } catch (error) {
      showToast({ title: "Tenant save failed", description: (error as Error).message, tone: "error" });
    } finally {
      setSaving(false);
    }
  }

  async function runHealthChecks() {
    if (!credentials) {
      return;
    }
    try {
      const jira = await testAtlassian(credentials, params.tenantId);
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
      showToast({ title: `Tenant ${action}d`, description: updated.tenant_id, tone: "success" });
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
      showToast({ title: `Tenant ${action} failed`, description: (error as Error).message, tone: "error" });
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
      const updated = await updateTenantDiscord(credentials, tenant.tenant_id, {
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
      showToast({ title: "Discord settings saved", tone: "success" });
    } catch (error) {
      showToast({ title: "Discord settings save failed", description: (error as Error).message, tone: "error" });
    } finally {
      setSaving(false);
    }
  }

  async function saveObservabilitySettings() {
    if (!credentials || !tenant) {
      return;
    }
    if (legalHoldEnabled && !legalHoldReason.trim()) {
      setStatusLine("Legal hold reason is required when legal hold is enabled.");
      return;
    }
    setSaving(true);
    try {
      const updated = await updateTenantObservability(credentials, tenant.tenant_id, {
        observability: {
          audit_retention_days: Number(auditRetentionDays || "365") || 365,
          audit_export_enabled: auditExportEnabled,
          legal_hold_enabled: legalHoldEnabled,
          legal_hold_reason: legalHoldEnabled ? legalHoldReason.trim() : null,
        },
      });
      setTenant(updated);
      setAuditRetentionDays(String(updated.policy.observability?.audit_retention_days ?? 365));
      setAuditExportEnabled(updated.policy.observability?.audit_export_enabled ?? true);
      setLegalHoldEnabled(updated.policy.observability?.legal_hold_enabled ?? false);
      setLegalHoldReason(updated.policy.observability?.legal_hold_reason ?? "");
      showToast({ title: "Observability policy saved", tone: "success" });
    } catch (error) {
      showToast({ title: "Observability policy save failed", description: (error as Error).message, tone: "error" });
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
      const result = await startAtlassianConnect(credentials, { returnTo: "edit", tenantId: params.tenantId });
      window.location.href = result.authorize_url;
    } catch (error) {
      setStatusLine(`Unable to start Atlassian: ${(error as Error).message}`);
    }
  }

  async function handleProvisionJiraWebhook() {
    if (!credentials) {
      return;
    }
    setJiraWebhookBusy(true);
    try {
      const result = await provisionJiraWebhook(credentials, params.tenantId);
      showToast({ title: "Jira webhook provisioned", description: result.details, tone: "success" });
      await loadJiraWebhookDiagnostics();
      await loadTenant();
    } catch (error) {
      showToast({ title: "Jira webhook provision failed", description: (error as Error).message, tone: "error" });
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
      showToast({ title: "Jira webhook reset", description: result.details, tone: "success" });
      await loadJiraWebhookDiagnostics();
      await loadTenant();
    } catch (error) {
      showToast({ title: "Jira webhook reset failed", description: (error as Error).message, tone: "error" });
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
      const result = await disconnectAtlassian(credentials, params.tenantId);
      showToast({ title: "Atlassian disconnected", description: result.details, tone: "success" });
      await loadJiraWebhookDiagnostics();
      await loadTenant();
    } catch (error) {
      showToast({ title: "Atlassian disconnect failed", description: (error as Error).message, tone: "error" });
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
      showToast({ title: "Project option sources refreshed", tone: "success" });
    } catch (error) {
      showToast({ title: "Project option refresh failed", description: (error as Error).message, tone: "error" });
      if (tenant?.jira.connection_id) {
        await loadNotifications();
      }
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
      showToast({ title: "Project created", description: payload.name, tone: "success" });
    } catch (error) {
      showToast({ title: "Project create failed", description: (error as Error).message, tone: "error" });
    } finally {
      setProjectsBusy(false);
    }
  }

  async function handleUpdateProjectConfiguration(projectId: string, payload: ProjectConfigurationUpdatePayload) {
    if (!credentials) {
      return;
    }
    setProjectsBusy(true);
    try {
      await updateProjectConfiguration(credentials, params.tenantId, projectId, payload);
      const refreshed = await listProjects(credentials, params.tenantId);
      setProjects(refreshed);
      showToast({ title: "Project updated", description: projectId, tone: "success" });
    } catch (error) {
      showToast({ title: "Project update failed", description: (error as Error).message, tone: "error" });
    } finally {
      setProjectsBusy(false);
    }
  }

  async function handleUpdateProjectArchiveState(projectId: string, payload: ProjectArchiveUpdatePayload) {
    if (!credentials) {
      return;
    }
    setProjectsBusy(true);
    try {
      await updateProjectArchiveState(credentials, params.tenantId, projectId, payload);
      const refreshed = await listProjects(credentials, params.tenantId);
      setProjects(refreshed);
      showToast({ title: payload.is_archived ? "Project archived" : "Project unarchived", description: projectId, tone: "success" });
    } catch (error) {
      showToast({ title: "Project archive update failed", description: (error as Error).message, tone: "error" });
    } finally {
      setProjectsBusy(false);
    }
  }

  if (loading) {
    return (
      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="px-6 pt-6">
          <Skeleton className="h-6 w-64" />
          <Skeleton className="mt-2 h-4 w-80" />
        </div>
        <div className="space-y-3 p-6">
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
        </div>
      </div>
    );
  }

  if (!tenant) {
    return (
      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="px-6 pt-6">
          <h2 className="text-base font-semibold">Tenant Not Found</h2>
          <p className="mt-1 text-sm text-muted-foreground">The requested tenant could not be loaded.</p>
        </div>
        <div className="p-6">
          <Button asChild>
            <Link href="/tenants/select">Back to Tenants</Link>
          </Button>
        </div>
      </div>
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
  const jiraReauthNotification =
    notifications.find(
      (notification) =>
        notification.kind === "reauth_required" &&
        notification.scope_type === "jira_connection" &&
        notification.scope_id === tenant.jira.connection_id
    ) ?? null;

  function notificationBadgeVariant(notification: AdminNotificationRecord): "destructive" | "warning" | "info" | "outline" {
    const severity = notification.severity.toUpperCase();
    if (severity === "CRITICAL" || severity === "HIGH") {
      return "destructive";
    }
    if (severity === "MEDIUM" || severity === "WARNING") {
      return "warning";
    }
    if (notification.status === "resolved") {
      return "outline";
    }
    return "info";
  }

  return (
    <div className="space-y-6">
      {statusLine ? <p className={cn("rounded-xl border px-4 py-3 text-sm", statusClasses)}>{statusLine}</p> : null}

      {section === "setup" ? (
        <div className="overflow-hidden rounded-2xl border bg-background">
          <div className="px-6 pt-6">
            <h2 className="text-base font-semibold">Setup Flow</h2>
          </div>
          <div className="space-y-3 p-6 text-sm">
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
                <Link href={getTenantSettingsRoute(tenant.tenant_id, "atlassian")}>Open Atlassian</Link>
              </Button>
            </div>
          </div>
        </div>
      ) : null}

      {section === "atlassian" ? (
        <div className="overflow-hidden rounded-2xl border bg-background">
          <div className="px-6 pt-6">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-base font-semibold">Atlassian Integration</h2>
              {jiraReauthNotification ? (
                <Badge variant={notificationBadgeVariant(jiraReauthNotification)}>Reauth required</Badge>
              ) : null}
            </div>
          </div>
          <div className="space-y-3 p-6 text-sm">
            <p>
              <strong>Status:</strong> {jiraConnected ? "Connected" : "Not connected"}
            </p>
            {jiraReauthNotification ? (
              <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-900">
                <p className="font-medium">{jiraReauthNotification.title}</p>
                <p className="mt-1 text-red-800">{jiraReauthNotification.detail}</p>
              </div>
            ) : null}
            <p>
              <strong>Connection ID:</strong> {tenant.jira.connection_id || "-"}
            </p>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" onClick={() => void connectJira()}>
                <Link2 className="mr-2 h-4 w-4" />
                {jiraConnected ? "Reconnect Atlassian" : "Connect Atlassian"}
              </Button>
              <Button variant="outline" disabled={jiraWebhookBusy} onClick={() => void handleDisconnectJira()}>
                Disconnect Atlassian
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
                <strong>Last received:</strong> {formatTimestamp(jiraWebhook?.last_received_at, "-")}
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
          </div>
        </div>
      ) : null}

      {section === "github" ? (
        <div className="overflow-hidden rounded-2xl border bg-background">
          <div className="px-6 pt-6">
            <h2 className="text-base font-semibold">GitHub Integration</h2>
          </div>
          <div className="space-y-4 p-6 text-sm">
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
          </div>
        </div>
      ) : null}

      {section === "projects" ? (
        <div className="overflow-hidden rounded-2xl border bg-background">
          <div className="px-6 pt-6">
            <h2 className="text-base font-semibold">Projects</h2>
          </div>
          <div className="p-6">
            <ProjectsManager
              projects={projects}
              repositories={githubRepositories}
              jiraProjects={jiraProjects}
              busy={projectsBusy || repositoriesLoading}
              onRefreshOptions={() => void refreshProjectSources()}
              onCreateProject={handleCreateProject}
              onUpdateProjectConfiguration={handleUpdateProjectConfiguration}
              onUpdateProjectArchiveState={handleUpdateProjectArchiveState}
            />
          </div>
        </div>
      ) : null}

      {section === "discord" ? (
        <div className="overflow-hidden rounded-2xl border bg-background">
          <div className="px-6 pt-6">
            <h2 className="text-base font-semibold">Discord Integration</h2>
          </div>
          <div className="space-y-4 p-6">
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
              <div className="rounded-xl border bg-background px-4 py-3 text-xs text-muted-foreground">
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
              <div className="rounded-xl border bg-background px-4 py-3 text-xs text-muted-foreground">
                Live voice rooms are configured per project on the project Discord page. Onboarding joins use the tenant onboarding channel.
              </div>
              <Button onClick={() => void saveDiscordSettings()} disabled={saving}>
                {saving ? "Saving..." : "Save"}
              </Button>
            </div>
          </div>
        </div>
      ) : null}

      {section === "config" ? (
        <div className="overflow-hidden rounded-2xl border bg-background">
          <div className="px-6 pt-6">
            <h2 className="text-base font-semibold">Workspace configuration</h2>
          </div>
          <div className="p-6">
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
          </div>
        </div>
      ) : null}

      {section === "observability" ? (
        <div className="overflow-hidden rounded-2xl border bg-background">
          <div className="px-6 pt-6">
            <h2 className="text-base font-semibold">Observability policy</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Configure tenant-level audit retention, export access, and legal hold. These settings control the durable audit plane, not live telemetry backend internals.
            </p>
          </div>
          <div className="space-y-5 p-6">
            <div className="grid gap-5 md:grid-cols-2">
              <div className="space-y-2">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Audit retention</label>
                <select
                  className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                  value={auditRetentionDays}
                  onChange={(event) => setAuditRetentionDays(event.target.value)}
                  disabled={saving}
                >
                  {AUDIT_RETENTION_OPTIONS.map((option) => (
                    <option key={option.value} value={String(option.value)}>
                      {option.label}
                    </option>
                  ))}
                </select>
                <p className="text-xs text-muted-foreground">
                  Audit history older than this is pruned automatically unless legal hold is active.
                </p>
              </div>
              <div className="space-y-3 rounded-xl border p-4 text-sm">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    className="h-4 w-4 rounded border-input"
                    checked={auditExportEnabled}
                    onChange={(event) => setAuditExportEnabled(event.target.checked)}
                    disabled={saving}
                  />
                  Allow audit export
                </label>
                <p className="text-xs text-muted-foreground">
                  Controls whether tenant-scoped audit history can be exported from the admin API.
                </p>
              </div>
            </div>

            <div className="space-y-3 rounded-xl border p-4">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  className="h-4 w-4 rounded border-input"
                  checked={legalHoldEnabled}
                  onChange={(event) => setLegalHoldEnabled(event.target.checked)}
                  disabled={saving}
                />
                Enable legal hold
              </label>
              <p className="text-xs text-muted-foreground">
                Prevents audit retention pruning for this tenant until the hold is cleared.
              </p>
              <div className="space-y-2">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Legal hold reason</label>
                <Textarea
                  value={legalHoldReason}
                  onChange={(event) => setLegalHoldReason(event.target.value)}
                  placeholder="Example: Customer litigation hold requested on 2026-04-20."
                  className="min-h-[110px]"
                  disabled={saving || !legalHoldEnabled}
                />
              </div>
            </div>

            <div className="flex justify-end">
              <Button onClick={() => void saveObservabilitySettings()} disabled={saving}>
                {saving ? "Saving..." : "Save observability policy"}
              </Button>
            </div>
          </div>
        </div>
      ) : null}

      {section === "danger" ? (
        <div className="space-y-6">
          <div className="overflow-hidden rounded-2xl border bg-background">
            <div className="px-6 pt-6">
              <h2 className="text-base font-semibold">Danger zone</h2>
            </div>
            <div className="divide-y">
              <div className="flex flex-wrap items-center justify-between gap-4 px-6 py-5">
                <div className="space-y-0.5">
                  <p className="text-sm font-medium">{tenant.is_enabled ? "Archive this workspace" : "Unarchive this workspace"}</p>
                  <p className="text-sm text-muted-foreground">
                    {tenant.is_enabled
                      ? "Archiving disables the workspace immediately and schedules permanent deletion in 60 days."
                      : `This workspace is archived. Scheduled purge: ${tenant.purge_after_at ?? "not scheduled"}.`}
                  </p>
                </div>
                <Button
                  variant="outline"
                  className={
                    tenant.is_enabled
                      ? "border-red-300 text-red-600 hover:bg-red-50 hover:text-red-700"
                      : undefined
                  }
                  onClick={() => {
                    if (tenant.is_enabled) {
                      setShowArchiveConfirm(true);
                      setArchiveConfirmationName("");
                    } else {
                      void handleArchiveToggle();
                    }
                  }}
                  disabled={archiveBusy}
                >
                  {tenant.is_enabled ? "Archive workspace…" : "Unarchive workspace"}
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
                <h3 className="text-lg font-semibold">Archive workspace</h3>
                <p className="mt-2 text-sm text-muted-foreground">
                  This will disable <span className="font-medium text-foreground">{tenant.name}</span> immediately
                  and schedule permanent deletion in 60 days.
                </p>
                <div className="mt-4 space-y-2">
                  <p className="text-sm">
                    To confirm, type <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-sm">{tenant.name}</span> below.
                  </p>
                  <Input
                    value={archiveConfirmationName}
                    onChange={(event) => setArchiveConfirmationName(event.target.value)}
                    placeholder={tenant.name}
                    autoFocus
                    disabled={archiveBusy}
                  />
                </div>
                <div className="mt-6 flex justify-end gap-3">
                  <Button
                    variant="outline"
                    onClick={() => {
                      setShowArchiveConfirm(false);
                      setArchiveConfirmationName("");
                    }}
                    disabled={archiveBusy}
                  >
                    Cancel
                  </Button>
                  <Button
                    className="border-red-300 bg-red-600 text-white hover:bg-red-700 hover:text-white"
                    onClick={() => {
                      void handleArchiveToggle().then(() => setShowArchiveConfirm(false));
                    }}
                    disabled={
                      archiveBusy ||
                      archiveConfirmationName.trim() !== tenant.name.trim()
                    }
                  >
                    {archiveBusy ? "Archiving…" : "Archive workspace"}
                  </Button>
                </div>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {section === "health" ? (
        <div className="overflow-hidden rounded-2xl border bg-background">
          <div className="px-6 pt-6">
            <h2 className="text-base font-semibold">Integration Health</h2>
          </div>
          <div className="space-y-3 p-6 text-sm">
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" onClick={() => void runHealthChecks()}>
                Run Health Checks
              </Button>
              <Button variant="secondary" onClick={() => void runReadyPreview()}>
                Preview Ready Gate
              </Button>
            </div>
            <div className="rounded-xl border bg-background px-4 py-3">
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
          </div>
        </div>
      ) : null}

      {section === "notifications" ? (
        <div className="overflow-hidden rounded-2xl border bg-background">
          <div className="px-6 pt-6">
            <h2 className="text-base font-semibold">Notifications</h2>
          </div>
          <div className="p-6 text-sm">
            <div className="overflow-hidden rounded-xl border">
              <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-3 border-b px-3 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                <span>Notification</span>
                <span>Status</span>
                <span>Action</span>
              </div>
              <ul className="divide-y">
                {notifications.length ? (
                  notifications.map((notification) => (
                    <li
                      key={notification.notification_id}
                      className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-3 px-3 py-3"
                    >
                      <div className="space-y-0.5">
                        <p className="font-medium">{notification.title}</p>
                        <p className="text-xs text-muted-foreground">{notification.detail}</p>
                      </div>
                      <Badge variant={notificationBadgeVariant(notification)}>{notification.status}</Badge>
                      {notification.kind === "reauth_required" ? (
                        <Button size="sm" variant="outline" onClick={() => void connectJira()}>
                          Reconnect Atlassian
                        </Button>
                      ) : (
                        <Button asChild size="sm" variant="outline">
                          <Link href={getTenantSettingsRoute(tenant.tenant_id, "atlassian")}>Review</Link>
                        </Button>
                      )}
                    </li>
                  ))
                ) : (
                  <li className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-3 px-3 py-3">
                    <div className="space-y-0.5">
                      <p className="font-medium">Jira Webhook Delivery</p>
                      <p className="text-xs text-muted-foreground">{jiraWebhookStatus.detail}</p>
                    </div>
                    <Badge variant={jiraWebhookStatus.label === "error" ? "destructive" : jiraWebhookStatus.label === "warning" ? "warning" : "outline"}>
                      {jiraWebhookStatus.label}
                    </Badge>
                    <Button asChild size="sm" variant="outline">
                      <Link href={getTenantSettingsRoute(tenant.tenant_id, "atlassian")}>Review</Link>
                    </Button>
                  </li>
                )}
              </ul>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
