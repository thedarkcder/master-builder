"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, Check, Copy, RefreshCcw } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { DEFAULT_API_BASE_URL } from "@/lib/auth-constants";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  getJiraWebhookDiagnostics,
  getTenant,
  testGithub,
  testJira,
  type JiraWebhookDiagnosticsRecord,
  type TenantRecord
} from "@/lib/api";

type WebhookDoc = {
  key: string;
  name: string;
  description: string;
  url: string;
};

type HealthStatus = {
  ok: boolean;
  details: string;
};

export default function TenantWebhooksPage() {
  const { credentials } = useAuth();
  const params = useParams<{ tenantId: string }>();
  const tenantId = params.tenantId;
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [statusLine, setStatusLine] = useState("Copy expected values and compare with your provider config.");
  const [tenant, setTenant] = useState<TenantRecord | null>(null);
  const [diagnostics, setDiagnostics] = useState<JiraWebhookDiagnosticsRecord | null>(null);
  const [jiraHealth, setJiraHealth] = useState<HealthStatus | null>(null);
  const [githubHealth, setGithubHealth] = useState<HealthStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<string | null>(null);

<<<<<<< HEAD
  const apiBaseUrl = (credentials?.apiBaseUrl || DEFAULT_API_BASE_URL).replace(/\/$/, "");
  const docs: WebhookDoc[] = useMemo(() => [
=======
  const apiBaseUrl = (credentials?.apiBaseUrl || DEFAULT_API_BASE_URL).replace(/\/$/, "");
  const docs: WebhookDoc[] = [
>>>>>>> origin/staging
    {
      key: "jira",
      name: "Jira Webhook URL",
      description: "Use this URL in Jira webhook settings.",
      url: `${apiBaseUrl}/jira/webhook/${tenantId}`
    },
    {
      key: "github",
      name: "GitHub Webhook URL",
      description: "Use this URL for GitHub App/repo webhook events.",
      url: `${apiBaseUrl}/github/webhook`
    },
    {
      key: "discord",
      name: "Discord Interactions URL",
      description: "Set this in Discord Developer Portal > Interactions Endpoint URL.",
      url: `${apiBaseUrl}/discord/interactions`
    }
  ], [apiBaseUrl, tenantId]);

  const jiraExpectedJql = tenant?.jira.ready_jql?.trim() || `project in (${(tenant?.jira.project_keys || []).join(", ")})`;
  const jiraExpectedEvents = "issue_created, issue_updated, issue_deleted, comment_created, comment_updated";
  const githubExpectedEvents = "pull_request, pull_request_review, pull_request_review_comment, check_suite, check_run";

  const refreshDiagnostics = useCallback(async () => {
    if (!credentials) {
      return;
    }
    setLoading(true);
    setStatusLine("Refreshing webhook diagnostics and integration health checks...");
    try {
      const [tenantResult, diagnosticsResult, jiraHealthResult, githubHealthResult] = await Promise.allSettled([
        getTenant(credentials, tenantId),
        getJiraWebhookDiagnostics(credentials, tenantId),
        testJira(credentials, tenantId),
        testGithub(credentials, tenantId)
      ]);

      if (tenantResult.status === "fulfilled") {
        setTenant(tenantResult.value);
      }
      if (diagnosticsResult.status === "fulfilled") {
        setDiagnostics(diagnosticsResult.value);
      }
      if (jiraHealthResult.status === "fulfilled") {
        setJiraHealth(jiraHealthResult.value);
      } else {
        setJiraHealth({ ok: false, details: jiraHealthResult.reason instanceof Error ? jiraHealthResult.reason.message : "Jira health check failed" });
      }
      if (githubHealthResult.status === "fulfilled") {
        setGithubHealth(githubHealthResult.value);
      } else {
        setGithubHealth({ ok: false, details: githubHealthResult.reason instanceof Error ? githubHealthResult.reason.message : "GitHub health check failed" });
      }

      setLastRefreshedAt(new Date().toISOString());
      setStatusLine("Diagnostics refreshed.");
    } catch (error) {
      setStatusLine(`Refresh failed: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [credentials, tenantId]);

  async function copyWebhook(key: string, url: string) {
    try {
      await navigator.clipboard.writeText(url);
      setCopiedKey(key);
      setStatusLine(`Copied: ${url}`);
      window.setTimeout(() => {
        setCopiedKey((current) => (current === key ? null : current));
      }, 1200);
    } catch (error) {
      setStatusLine(`Copy failed: ${(error as Error).message}`);
    }
  }

  useEffect(() => {
    void refreshDiagnostics();
  }, [refreshDiagnostics]);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle>Tenant Webhooks: {tenantId}</CardTitle>
            <CardDescription>Verification checklist and expected provider configuration.</CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" type="button" onClick={() => void refreshDiagnostics()} disabled={loading}>
              <RefreshCcw className="mr-2 h-4 w-4" />
              {loading ? "Refreshing" : "Refresh"}
            </Button>
            <Button variant="outline" asChild>
              <Link href={`/tenants/${encodeURIComponent(tenantId)}/edit`}>
                <ArrowLeft className="mr-2 h-4 w-4" />
                Back To Tenant
              </Link>
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="rounded-md border bg-muted/30 px-3 py-2 text-sm text-foreground">
          {statusLine}
          {lastRefreshedAt ? ` Last refreshed: ${new Date(lastRefreshedAt).toLocaleString()}.` : ""}
        </p>

        <div className="grid gap-3 md:grid-cols-3">
          <div className="rounded-md border p-3 text-sm">
            <p className="font-medium">Jira health</p>
            <p className={jiraHealth?.ok ? "text-emerald-700" : "text-amber-700"}>
              {jiraHealth ? (jiraHealth.ok ? "Healthy" : "Needs attention") : "Checking..."}
            </p>
            <p className="text-xs text-muted-foreground">{jiraHealth?.details || "Running health check..."}</p>
          </div>
          <div className="rounded-md border p-3 text-sm">
            <p className="font-medium">GitHub health</p>
            <p className={githubHealth?.ok ? "text-emerald-700" : "text-amber-700"}>
              {githubHealth ? (githubHealth.ok ? "Healthy" : "Needs attention") : "Checking..."}
            </p>
            <p className="text-xs text-muted-foreground">{githubHealth?.details || "Running health check..."}</p>
          </div>
          <div className="rounded-md border p-3 text-sm">
            <p className="font-medium">Recent Jira delivery</p>
            <p className={diagnostics?.recent_delivery_ok ? "text-emerald-700" : "text-amber-700"}>
              {diagnostics ? (diagnostics.recent_delivery_ok ? "Recent delivery detected" : "No recent delivery") : "Checking..."}
            </p>
            <p className="text-xs text-muted-foreground">
              {diagnostics?.last_received_at
                ? `Last issue: ${diagnostics.last_issue_key || "n/a"}`
                : "Trigger a Jira status change and refresh."}
            </p>
          </div>
        </div>

        <div className="space-y-3">
          {docs.map((doc) => (
            <div
              key={doc.key}
              className="grid gap-2 rounded-md border p-3 md:grid-cols-[220px_1fr_auto] md:items-center"
            >
              <div>
                <p className="text-sm font-medium">{doc.name}</p>
                <p className="text-xs text-muted-foreground">{doc.description}</p>
              </div>
              <code className="block overflow-x-auto rounded bg-muted/60 px-2 py-1 text-xs">{doc.url}</code>
              <Button type="button" variant="outline" size="sm" onClick={() => void copyWebhook(doc.key, doc.url)}>
                {copiedKey === doc.key ? (
                  <>
                    <Check className="mr-2 h-4 w-4" />
                    Copied
                  </>
                ) : (
                  <>
                    <Copy className="mr-2 h-4 w-4" />
                    Copy
                  </>
                )}
              </Button>
            </div>
          ))}
        </div>

        <div className="space-y-3 rounded-md border p-3">
          <p className="text-sm font-medium">Expected Jira webhook configuration</p>
          <div className="space-y-1 text-xs text-muted-foreground">
            <p>Events: {jiraExpectedEvents}</p>
            <p>Ready JQL: {jiraExpectedJql}</p>
            <p>Managed webhook IDs: {diagnostics?.managed_webhook_ids?.length ? diagnostics.managed_webhook_ids.join(", ") : "none"}</p>
            <p>Last delivery ID: {diagnostics?.last_delivery_id || "none"}</p>
            <p>Last error: {diagnostics?.last_error || "none"}</p>
          </div>
        </div>

        <div className="space-y-3 rounded-md border p-3">
          <p className="text-sm font-medium">Expected GitHub webhook configuration</p>
          <div className="space-y-1 text-xs text-muted-foreground">
            <p>Events: {githubExpectedEvents}</p>
            <p>Installation ID: {tenant?.github.installation_id || "not connected"}</p>
            <p>Mode: {tenant?.github.mode || "unknown"}</p>
          </div>
        </div>

        <div className="space-y-2 rounded-md border p-3">
          <p className="text-sm font-medium">Verification checklist</p>
          <ol className="list-decimal space-y-1 pl-4 text-xs text-muted-foreground">
            <li>Copy the Jira webhook URL above and confirm Jira points to that exact value.</li>
            <li>Confirm Jira events include: {jiraExpectedEvents}.</li>
            <li>Confirm GitHub webhook URL and events include: {githubExpectedEvents}.</li>
            <li>Trigger a Jira status change to a ready status and refresh this page.</li>
            <li>Verify recent delivery is detected and no webhook error is present.</li>
          </ol>
        </div>
      </CardContent>
    </Card>
  );
}
