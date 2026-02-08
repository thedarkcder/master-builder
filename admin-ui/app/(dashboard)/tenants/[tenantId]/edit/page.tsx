"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { KeyRound, Link2 } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { TenantForm } from "@/components/tenant-form";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  disconnectJira,
  getTenant,
  getJiraWebhookDiagnostics,
  previewReadyGate,
  provisionJiraWebhook,
  type ReadyGatePreviewRecord,
  type JiraWebhookDiagnosticsRecord,
  startJiraConnect,
  startGitHubInstall,
  resetJiraWebhook,
  testGithub,
  testJira,
  updateTenant,
  type TenantRecord,
  type TenantUpdatePayload
} from "@/lib/api";
import { recordToFormValues } from "@/lib/tenant-form";

export default function EditTenantPage() {
  const params = useParams<{ tenantId: string }>();
  const searchParams = useSearchParams();
  const { credentials, ready } = useAuth();

  const [tenant, setTenant] = useState<TenantRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [statusLine, setStatusLine] = useState("Loading tenant...");
  const [readyPreview, setReadyPreview] = useState<ReadyGatePreviewRecord | null>(null);
  const [jiraWebhook, setJiraWebhook] = useState<JiraWebhookDiagnosticsRecord | null>(null);
  const [jiraWebhookBusy, setJiraWebhookBusy] = useState(false);

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

  async function loadTenant() {
    if (!credentials) {
      return;
    }
    setLoading(true);
    try {
      const payload = await getTenant(credentials, params.tenantId);
      setTenant(payload);
      setStatusLine(`Loaded ${payload.tenant_id}.`);
      await loadJiraWebhookDiagnostics();
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
      const result = await startGitHubInstall(credentials, params.tenantId);
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

  if (loading) {
    return <p className="rounded-md border bg-card p-4 text-sm text-muted-foreground">Loading tenant configuration...</p>;
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

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle>Edit Tenant: {tenant.tenant_id}</CardTitle>
            <CardDescription>Update tenant configuration through structured form fields.</CardDescription>
          </div>
          <div className="flex gap-2">
            <Button asChild variant="outline">
              <Link href="/secrets">
                <KeyRound className="mr-2 h-4 w-4" />
                Manage Secrets
              </Link>
            </Button>
            <Button variant="outline" onClick={() => void connectJira()}>
              <Link2 className="mr-2 h-4 w-4" />
              Connect Jira
            </Button>
            <Button variant="outline" onClick={() => void connectGitHubApp()}>
              <Link2 className="mr-2 h-4 w-4" />
              Install GitHub App
            </Button>
            <Button variant="secondary" onClick={() => void runHealthChecks()}>
              Run Health Checks
            </Button>
            <Button variant="secondary" onClick={() => void runReadyPreview()}>
              Preview Ready Gate
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="rounded-md border bg-muted/30 p-3 text-sm text-muted-foreground">{statusLine}</p>
        <Card>
          <CardHeader>
            <CardTitle>Jira Webhook Lifecycle</CardTitle>
            <CardDescription>Provision, reset, and diagnose tenant Jira webhook delivery.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <p>
              <strong>Webhook URL:</strong> {jiraWebhook?.webhook_url ?? "Loading..."}
            </p>
            <p>
              <strong>Connected:</strong> {jiraWebhook?.connected ? "yes" : "no"}
            </p>
            <p>
              <strong>Managed webhook IDs:</strong>{" "}
              {jiraWebhook?.managed_webhook_ids.length ? jiraWebhook.managed_webhook_ids.join(", ") : "-"}
            </p>
            <p>
              <strong>Last provisioned:</strong> {jiraWebhook?.last_provisioned_at ?? "-"}
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
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" disabled={jiraWebhookBusy} onClick={() => void handleProvisionJiraWebhook()}>
                Provision Webhook
              </Button>
              <Button variant="secondary" disabled={jiraWebhookBusy} onClick={() => void handleResetJiraWebhook()}>
                Reset Webhook
              </Button>
              <Button variant="outline" disabled={jiraWebhookBusy} onClick={() => void handleDisconnectJira()}>
                Disconnect Jira
              </Button>
            </div>
          </CardContent>
        </Card>
        <TenantForm mode="edit" initialValues={recordToFormValues(tenant)} onSubmit={handleSave} submitting={saving} />
        {readyPreview ? (
          <Card>
            <CardHeader>
              <CardTitle>Ready Gate Preview</CardTitle>
              <CardDescription>{readyPreview.guidance}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
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
            </CardContent>
          </Card>
        ) : null}
      </CardContent>
    </Card>
  );
}
