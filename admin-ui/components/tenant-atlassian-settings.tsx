"use client";

import { useEffect, useMemo, useState } from "react";
import { Link2 } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast-provider";
import {
  disconnectAtlassian,
  getTenant,
  getJiraWebhookDiagnostics,
  listTenantNotifications,
  provisionJiraWebhook,
  resetJiraWebhook,
  startAtlassianConnect,
  type AdminNotificationRecord,
  type JiraWebhookDiagnosticsRecord,
  type TenantRecord
} from "@/lib/api";
import { canAccessPlatformAdmin } from "@/lib/auth-routing";
import { formatTimestamp } from "@/lib/datetime";

type TenantAtlassianSettingsProps = {
  tenant: TenantRecord;
  onTenantUpdated: (tenant: TenantRecord) => void;
  onStatus: (message: string) => void;
};

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

export function TenantAtlassianSettings({ tenant, onTenantUpdated, onStatus }: TenantAtlassianSettingsProps) {
  const { credentials, principal } = useAuth();
  const { showToast } = useToast();
  const isPlatformAdmin = canAccessPlatformAdmin(principal);
  const [jiraWebhook, setJiraWebhook] = useState<JiraWebhookDiagnosticsRecord | null>(null);
  const [notifications, setNotifications] = useState<AdminNotificationRecord[]>([]);
  const [busy, setBusy] = useState(false);

  const jiraConnected = Boolean(tenant.jira.connection_id && tenant.jira.connection_id.trim());
  const jiraReauthNotification = useMemo(
    () =>
      notifications.find(
        (notification) =>
          notification.kind === "reauth_required" &&
          notification.scope_type === "jira_connection" &&
          notification.scope_id === tenant.jira.connection_id
      ) ?? null,
    [notifications, tenant.jira.connection_id]
  );

  async function loadDiagnostics(): Promise<void> {
    if (!credentials || !isPlatformAdmin) {
      setJiraWebhook(null);
      setNotifications([]);
      return;
    }
    try {
      const [diagnostics, notificationPayload] = await Promise.all([
        getJiraWebhookDiagnostics(credentials, tenant.tenant_id),
        listTenantNotifications(credentials, tenant.tenant_id)
      ]);
      setJiraWebhook(diagnostics);
      setNotifications(notificationPayload.notifications);
    } catch (error) {
      onStatus(`Failed to load Atlassian diagnostics: ${(error as Error).message}`);
    }
  }

  useEffect(() => {
    void loadDiagnostics();
  }, [credentials, isPlatformAdmin, tenant.tenant_id]);

  async function connectJira(): Promise<void> {
    if (!credentials) {
      return;
    }
    try {
      const result = await startAtlassianConnect(credentials, { returnTo: "edit", tenantId: tenant.tenant_id });
      window.location.href = result.authorize_url;
    } catch (error) {
      onStatus(`Unable to start Atlassian: ${(error as Error).message}`);
    }
  }

  async function handleProvisionJiraWebhook(): Promise<void> {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      const result = await provisionJiraWebhook(credentials, tenant.tenant_id);
      showToast({ title: "Jira webhook provisioned", description: result.details, tone: "success" });
      onTenantUpdated(await getTenant(credentials, tenant.tenant_id));
      await loadDiagnostics();
    } catch (error) {
      showToast({ title: "Jira webhook provision failed", description: (error as Error).message, tone: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function handleResetJiraWebhook(): Promise<void> {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      const result = await resetJiraWebhook(credentials, tenant.tenant_id);
      showToast({ title: "Jira webhook reset", description: result.details, tone: "success" });
      onTenantUpdated(await getTenant(credentials, tenant.tenant_id));
      await loadDiagnostics();
    } catch (error) {
      showToast({ title: "Jira webhook reset failed", description: (error as Error).message, tone: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function handleDisconnectJira(): Promise<void> {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      const result = await disconnectAtlassian(credentials, tenant.tenant_id);
      showToast({ title: "Atlassian disconnected", description: result.details, tone: "success" });
      onTenantUpdated(await getTenant(credentials, tenant.tenant_id));
      await loadDiagnostics();
    } catch (error) {
      showToast({ title: "Atlassian disconnect failed", description: (error as Error).message, tone: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
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
          <Button variant="outline" disabled={busy} onClick={() => void handleDisconnectJira()}>
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
              <Button variant="secondary" disabled={busy} onClick={() => void handleProvisionJiraWebhook()}>
                Provision Webhook
              </Button>
              <Button variant="secondary" disabled={busy} onClick={() => void handleResetJiraWebhook()}>
                Reset Webhook
              </Button>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
