"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { KeyRound, Link2 } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { TenantAtlassianSettings } from "@/components/tenant-atlassian-settings";
import { TenantDiscordSettings } from "@/components/tenant-discord-settings";
import { TenantForm } from "@/components/tenant-form";
import { TenantObservabilitySettings } from "@/components/tenant-observability-settings";
import { TenantProjectsSettings } from "@/components/tenant-projects-settings";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast-provider";
import {
  type AdminNotificationRecord,
  archiveTenant,
  listCodexModels,
  getTenant,
  getJiraWebhookDiagnostics,
  listTenantNotifications,
  previewReadyGate,
  type ReadyGatePreviewRecord,
  type JiraWebhookDiagnosticsRecord,
  startAtlassianConnect,
  startDiscordInstall,
  startGitHubInstall,
  testGithub,
  testAtlassian,
  unarchiveTenant,
  updateTenantConfiguration,
  updateTenantDiscord,
  updateTenantGithub,
  updateTenantPolicy,
  type TenantRecord,
  type TenantConfigurationUpdatePayload,
  type TenantDiscordUpdatePayload,
  type TenantGithubUpdatePayload,
  type TenantPolicyUpdatePayload,
} from "@/lib/api";
import { canAccessPlatformAdmin, getTenantArchiveConfirmationRoute, getTenantSettingsRoute } from "@/lib/auth-routing";
import { recordToFormValues } from "@/lib/tenant-form";
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
  const [codexModels, setCodexModels] = useState<{ id: string; label: string; description?: string | null }[]>([]);
  const [globalCodexModel, setGlobalCodexModel] = useState("");
  const [reasoningEfforts, setReasoningEfforts] = useState<{ id: string; label: string; description?: string | null }[]>([]);
  const [globalCodexReasoningEffort, setGlobalCodexReasoningEffort] = useState("");
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [archiveConfirmationName, setArchiveConfirmationName] = useState("");
  const [showArchiveConfirm, setShowArchiveConfirm] = useState(false);

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

  async function loadTenant() {
    if (!credentials) {
      return;
    }
    setLoading(true);
    try {
      const payload = await getTenant(credentials, params.tenantId);
      setTenant(payload);

      if (isPlatformAdmin && section === "config") {
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

      if (isPlatformAdmin && section === "notifications") {
        await Promise.all([loadJiraWebhookDiagnostics(), loadNotifications()]);
      } else {
        setJiraWebhook(null);
        setNotifications([]);
      }
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

  async function saveTenantConfiguration(payload: TenantConfigurationUpdatePayload): Promise<void> {
    if (!credentials || !tenant) {
      return;
    }
    setSaving(true);
    try {
      const updated = await updateTenantConfiguration(credentials, params.tenantId, payload);
      setTenant(updated);
      showToast({ title: "Workspace configuration saved", description: updated.tenant_id, tone: "success" });
    } catch (error) {
      showToast({ title: "Workspace configuration save failed", description: (error as Error).message, tone: "error" });
    } finally {
      setSaving(false);
    }
  }

  async function saveTenantGithub(payload: TenantGithubUpdatePayload): Promise<void> {
    if (!credentials || !tenant) {
      return;
    }
    setSaving(true);
    try {
      const updated = await updateTenantGithub(credentials, params.tenantId, payload);
      setTenant(updated);
      showToast({ title: "GitHub settings saved", description: updated.tenant_id, tone: "success" });
    } catch (error) {
      showToast({ title: "GitHub settings save failed", description: (error as Error).message, tone: "error" });
    } finally {
      setSaving(false);
    }
  }

  async function saveTenantPolicy(payload: TenantPolicyUpdatePayload): Promise<void> {
    if (!credentials || !tenant) {
      return;
    }
    setSaving(true);
    try {
      const updated = await updateTenantPolicy(credentials, params.tenantId, payload);
      setTenant(updated);
      showToast({ title: "Tenant policy saved", description: updated.tenant_id, tone: "success" });
    } catch (error) {
      showToast({ title: "Tenant policy save failed", description: (error as Error).message, tone: "error" });
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

  async function saveDiscordSettings(payload: TenantDiscordUpdatePayload) {
    if (!credentials || !tenant) {
      return;
    }
    setSaving(true);
    try {
      const updated = await updateTenantDiscord(credentials, tenant.tenant_id, payload);
      setTenant(updated);
      showToast({ title: "Discord settings saved", tone: "success" });
    } catch (error) {
      showToast({ title: "Discord settings save failed", description: (error as Error).message, tone: "error" });
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
              <li>Manage required integration vault entries.</li>
              <li>Connect integrations from the dedicated Jira and GitHub pages.</li>
              <li>Create project mappings (repo + Jira key) in Projects.</li>
              <li>Run health checks and preview ready gate.</li>
            </ol>
            <div className="flex flex-wrap gap-2 border-t pt-3">
              <Button asChild variant="outline">
                <Link href={`/${encodeURIComponent(tenant.tenant_id)}/secrets`}>
                  <KeyRound className="mr-2 h-4 w-4" />
                  Open Vault
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
        <TenantAtlassianSettings tenant={tenant} onTenantUpdated={setTenant} onStatus={setStatusLine} />
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
              submitScope="github"
              onSubmit={saveTenantGithub}
              submitting={saving}
              submitLabel="Save GitHub settings"
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
        <TenantProjectsSettings tenant={tenant} />
      ) : null}

      {section === "discord" ? (
        <TenantDiscordSettings
          tenant={tenant}
          saving={saving}
          onSave={saveDiscordSettings}
          onInstall={() => void connectDiscordInstall("edit")}
        />
      ) : null}

      {section === "config" ? (
        <div className="space-y-6">
          <div className="overflow-hidden rounded-2xl border bg-background">
            <div className="px-6 pt-6">
              <h2 className="text-base font-semibold">Workspace configuration</h2>
            </div>
            <div className="p-6">
              <TenantForm
                mode="edit"
                initialValues={recordToFormValues(tenant)}
                submitScope="configuration"
                onSubmit={saveTenantConfiguration}
                submitting={saving}
                submitLabel="Save workspace configuration"
                codexModels={codexModels}
                reasoningEfforts={reasoningEfforts}
                globalCodexModel={globalCodexModel}
                globalCodexReasoningEffort={globalCodexReasoningEffort}
                visibleSections={{
                  identity: true,
                  jira: false,
                  github: false,
                  repository: false,
                  policy: false,
                  discord: false
                }}
              />
            </div>
          </div>
          <div className="overflow-hidden rounded-2xl border bg-background">
            <div className="px-6 pt-6">
              <h2 className="text-base font-semibold">Tenant policy</h2>
            </div>
            <div className="p-6">
              <TenantForm
                mode="edit"
                initialValues={recordToFormValues(tenant)}
                submitScope="policy"
                onSubmit={saveTenantPolicy}
                submitting={saving}
                submitLabel="Save policy"
                codexModels={codexModels}
                reasoningEfforts={reasoningEfforts}
                globalCodexModel={globalCodexModel}
                globalCodexReasoningEffort={globalCodexReasoningEffort}
                visibleSections={{
                  identity: false,
                  jira: false,
                  github: false,
                  repository: false,
                  policy: true,
                  discord: false
                }}
              />
            </div>
          </div>
        </div>
      ) : null}

      {section === "observability" ? (
        <TenantObservabilitySettings tenant={tenant} onTenantUpdated={setTenant} onStatus={setStatusLine} />
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
