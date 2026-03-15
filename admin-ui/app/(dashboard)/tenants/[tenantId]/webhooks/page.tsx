"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, Check, CheckCircle2, Circle, Copy, RefreshCcw, XCircle } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { DEFAULT_API_BASE_URL } from "@/lib/auth-constants";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
  const [statusLine, setStatusLine] = useState("");
  const [tenant, setTenant] = useState<TenantRecord | null>(null);
  const [diagnostics, setDiagnostics] = useState<JiraWebhookDiagnosticsRecord | null>(null);
  const [jiraHealth, setJiraHealth] = useState<HealthStatus | null>(null);
  const [githubHealth, setGithubHealth] = useState<HealthStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<string | null>(null);

  const apiBaseUrl = (credentials?.apiBaseUrl || DEFAULT_API_BASE_URL).replace(/\/$/, "");
  const docs: WebhookDoc[] = useMemo(
    () => [
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
        description: "Set this in Discord Developer Portal › Interactions Endpoint URL.",
        url: `${apiBaseUrl}/discord/interactions`
      }
    ],
    [apiBaseUrl, tenantId]
  );

  const jiraExpectedJql =
    tenant?.jira.ready_jql?.trim() || `project in (${(tenant?.jira.project_keys || []).join(", ")})`;
  const jiraExpectedEvents =
    "issue_created, issue_updated, issue_deleted, comment_created, comment_updated";
  const githubExpectedEvents =
    "pull_request, pull_request_review, pull_request_review_comment, issue_comment, check_suite, check_run";

  const refreshDiagnostics = useCallback(async () => {
    if (!credentials) return;
    setLoading(true);
    setStatusLine("Refreshing...");
    try {
      const [tenantResult, diagnosticsResult, jiraHealthResult, githubHealthResult] =
        await Promise.allSettled([
          getTenant(credentials, tenantId),
          getJiraWebhookDiagnostics(credentials, tenantId),
          testJira(credentials, tenantId),
          testGithub(credentials, tenantId)
        ]);
      if (tenantResult.status === "fulfilled") setTenant(tenantResult.value);
      if (diagnosticsResult.status === "fulfilled") setDiagnostics(diagnosticsResult.value);
      setJiraHealth(
        jiraHealthResult.status === "fulfilled"
          ? jiraHealthResult.value
          : { ok: false, details: jiraHealthResult.reason instanceof Error ? jiraHealthResult.reason.message : "Jira health check failed" }
      );
      setGithubHealth(
        githubHealthResult.status === "fulfilled"
          ? githubHealthResult.value
          : { ok: false, details: githubHealthResult.reason instanceof Error ? githubHealthResult.reason.message : "GitHub health check failed" }
      );
      setLastRefreshedAt(new Date().toISOString());
      const failed = [
        tenantResult.status !== "fulfilled" ? "tenant config" : null,
        diagnosticsResult.status !== "fulfilled" ? "jira diagnostics" : null,
        jiraHealthResult.status !== "fulfilled" ? "jira health" : null,
        githubHealthResult.status !== "fulfilled" ? "github health" : null
      ].filter(Boolean);
      setStatusLine(failed.length > 0 ? `Completed with warnings: ${failed.join(", ")} failed.` : "Diagnostics refreshed.");
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
      window.setTimeout(() => setCopiedKey((c) => (c === key ? null : c)), 1200);
    } catch (error) {
      setStatusLine(`Copy failed: ${(error as Error).message}`);
    }
  }

  useEffect(() => {
    void refreshDiagnostics();
  }, [refreshDiagnostics]);

  const checklist = [
    {
      done: Boolean(jiraHealth?.ok),
      label: `Jira health check passed`
    },
    {
      done: Boolean(githubHealth?.ok),
      label: `GitHub health check passed`
    },
    {
      done: Boolean(diagnostics?.recent_delivery_ok),
      label: `Recent Jira webhook delivery detected`
    },
    {
      done: !diagnostics?.last_error,
      label: "No webhook delivery errors"
    }
  ];

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Webhooks</h1>
          <p className="text-sm text-muted-foreground">
            Integration health and expected provider configuration.
            {lastRefreshedAt ? ` Last refreshed ${new Date(lastRefreshedAt).toLocaleString()}.` : ""}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => void refreshDiagnostics()} disabled={loading}>
            <RefreshCcw className={`mr-1.5 h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            {loading ? "Refreshing" : "Refresh"}
          </Button>
          <Button variant="outline" size="sm" asChild>
            <Link href={`/tenants/${encodeURIComponent(tenantId)}/edit/integrations`}>
              <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />
              Settings
            </Link>
          </Button>
        </div>
      </div>

      {statusLine ? (
        <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p>
      ) : null}

      {/* Health status cards */}
      <div className="grid gap-3 sm:grid-cols-3">
        {[
          {
            title: "Jira",
            health: jiraHealth,
            detail: jiraHealth?.details || "Checking..."
          },
          {
            title: "GitHub",
            health: githubHealth,
            detail: githubHealth?.details || "Checking..."
          },
          {
            title: "Recent Jira Delivery",
            health: diagnostics
              ? { ok: diagnostics.recent_delivery_ok, details: diagnostics.last_issue_key ? `Last issue: ${diagnostics.last_issue_key}` : "No delivery yet" }
              : null,
            detail: diagnostics
              ? diagnostics.recent_delivery_ok
                ? `Last issue: ${diagnostics.last_issue_key || "n/a"}`
                : "Trigger a Jira status change and refresh."
              : "Checking..."
          }
        ].map((item) => {
          const ok = item.health?.ok;
          const Icon = ok ? CheckCircle2 : XCircle;
          return (
            <Card key={item.title}>
              <CardContent className="flex items-start gap-3 p-4">
                <Icon className={`h-5 w-5 flex-shrink-0 mt-0.5 ${ok ? "text-success" : ok === undefined ? "text-muted-foreground" : "text-warning"}`} />
                <div>
                  <p className="font-medium text-sm">{item.title}</p>
                  <p className={`text-sm ${ok ? "text-success" : ok === undefined ? "text-muted-foreground" : "text-warning"}`}>
                    {item.health ? (ok ? "Healthy" : "Needs attention") : "Checking..."}
                  </p>
                  <p className="mt-0.5 text-xs text-muted-foreground">{item.detail}</p>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      {/* Webhook URLs */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">Webhook URLs</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {docs.map((doc) => (
            <div
              key={doc.key}
              className="grid gap-2 rounded-lg border p-3 md:grid-cols-[200px_1fr_auto] md:items-center"
            >
              <div>
                <p className="text-sm font-medium">{doc.name}</p>
                <p className="text-xs text-muted-foreground">{doc.description}</p>
              </div>
              <code className="block overflow-x-auto rounded-md bg-muted px-3 py-2 font-mono text-xs">{doc.url}</code>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8"
                onClick={() => void copyWebhook(doc.key, doc.url)}
              >
                {copiedKey === doc.key ? (
                  <>
                    <Check className="mr-1.5 h-3.5 w-3.5 text-success" />
                    Copied
                  </>
                ) : (
                  <>
                    <Copy className="mr-1.5 h-3.5 w-3.5" />
                    Copy
                  </>
                )}
              </Button>
            </div>
          ))}
        </CardContent>
      </Card>

      {/* Config cards */}
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm">Expected Jira Configuration</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-xs text-muted-foreground">
            <p><span className="font-medium text-foreground">Events</span> {jiraExpectedEvents}</p>
            <p><span className="font-medium text-foreground">Ready JQL</span> {jiraExpectedJql}</p>
            <p>
              <span className="font-medium text-foreground">Managed webhook IDs</span>{" "}
              {diagnostics?.managed_webhook_ids?.length ? diagnostics.managed_webhook_ids.join(", ") : "none"}
            </p>
            <p><span className="font-medium text-foreground">Last delivery ID</span> {diagnostics?.last_delivery_id || "none"}</p>
            {diagnostics?.last_error ? (
              <p className="rounded border border-destructive/30 bg-destructive/10 px-2 py-1.5 text-destructive">
                <span className="font-medium">Last error</span> {diagnostics.last_error}
              </p>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm">Expected GitHub Configuration</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-xs text-muted-foreground">
            <p><span className="font-medium text-foreground">Events</span> {githubExpectedEvents}</p>
            <p>
              <span className="font-medium text-foreground">Installation ID</span>{" "}
              {tenant?.github.installation_id || "not connected"}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Verification checklist */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">Verification Checklist</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="space-y-2">
            {checklist.map((item) => {
              const Icon = item.done ? CheckCircle2 : Circle;
              return (
                <li key={item.label} className="flex items-center gap-2.5 text-sm">
                  <Icon className={`h-4 w-4 flex-shrink-0 ${item.done ? "text-success" : "text-muted-foreground/50"}`} />
                  <span className={item.done ? "text-foreground" : "text-muted-foreground"}>{item.label}</span>
                </li>
              );
            })}
            <li className="flex items-center gap-2.5 text-sm">
              <Circle className="h-4 w-4 flex-shrink-0 text-muted-foreground/50" />
              <span className="text-muted-foreground">
                Copy the Jira webhook URL above and confirm Jira points to that exact value.
              </span>
            </li>
            <li className="flex items-center gap-2.5 text-sm">
              <Circle className="h-4 w-4 flex-shrink-0 text-muted-foreground/50" />
              <span className="text-muted-foreground">
                Trigger a Jira status change to a ready status and refresh to verify delivery.
              </span>
            </li>
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
