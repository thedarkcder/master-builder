"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { CheckCircle2, KeyRound, Link2, RefreshCw } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { TenantForm } from "@/components/tenant-form";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  approveDiscordAllowlistRequest,
  disconnectGitHub,
  disconnectJira,
  getTenant,
  listDiscordAllowlistRequests,
  listGitHubRepositories,
  previewReadyGate,
  type DiscordAllowlistRequestRecord,
  type ReadyGatePreviewRecord,
  type GitHubRepositoryRecord,
  startJiraConnect,
  startGitHubInstall,
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
  const [repositoryOptions, setRepositoryOptions] = useState<GitHubRepositoryRecord[]>([]);
  const [repositoriesLoading, setRepositoriesLoading] = useState(false);
  const [allowlistRequests, setAllowlistRequests] = useState<DiscordAllowlistRequestRecord[]>([]);
  const [allowlistLoading, setAllowlistLoading] = useState(false);
  const [approvingUserId, setApprovingUserId] = useState<string | null>(null);

  async function loadRepositoryOptions(
    record: TenantRecord,
    options: { announceSuccess?: boolean } = {}
  ): Promise<string | null> {
    const announceSuccess = options.announceSuccess ?? false;
    if (!credentials || !record.github.installation_id) {
      setRepositoryOptions([]);
      return null;
    }
    setRepositoriesLoading(true);
    try {
      const repositories = await listGitHubRepositories(credentials, record.tenant_id);
      setRepositoryOptions(repositories);
      if (repositories.length === 0) {
        return "GitHub installation connected, but no repositories are accessible.";
      }
      if (announceSuccess) {
        return `Loaded ${repositories.length} GitHub repository option(s).`;
      }
      return null;
    } catch (error) {
      setRepositoryOptions([]);
      return `Unable to load GitHub repositories: ${(error as Error).message}`;
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
      const [repoStatus] = await Promise.all([
        loadRepositoryOptions(payload),
        loadAllowlistRequests(payload.tenant_id)
      ]);
      setStatusLine(repoStatus ?? `Loaded ${payload.tenant_id}.`);
    } catch (error) {
      setStatusLine(`Failed to load tenant: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  async function refreshRepositoryOptions() {
    if (!tenant) {
      return;
    }
    const repoStatus = await loadRepositoryOptions(tenant, { announceSuccess: true });
    if (repoStatus) {
      setStatusLine(repoStatus);
    }
  }

  async function loadAllowlistRequests(tenantId: string): Promise<void> {
    if (!credentials) {
      return;
    }
    setAllowlistLoading(true);
    try {
      const requests = await listDiscordAllowlistRequests(credentials, tenantId);
      setAllowlistRequests(requests);
    } catch {
      setAllowlistRequests([]);
    } finally {
      setAllowlistLoading(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) {
      void loadTenant();
    }
  }, [ready, credentials]);

  useEffect(() => {
    const githubInstallSuccess = searchParams.get("github_install") === "success";
    if (searchParams.get("jira_oauth") === "success") {
      setStatusLine("Jira OAuth callback received. Update project keys if needed, then save.");
    }
    if (!githubInstallSuccess || !ready || !credentials) {
      return;
    }

    let cancelled = false;
    void (async () => {
      try {
        const payload = await getTenant(credentials, params.tenantId);
        if (cancelled) {
          return;
        }
        setTenant(payload);
        const [repoStatus] = await Promise.all([
          loadRepositoryOptions(payload, { announceSuccess: true }),
          loadAllowlistRequests(payload.tenant_id)
        ]);
        if (cancelled) {
          return;
        }
        setStatusLine(repoStatus ?? "GitHub App install callback received. Installation connected.");
      } catch (error) {
        if (!cancelled) {
          setStatusLine(`GitHub install callback refresh failed: ${(error as Error).message}`);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [searchParams, ready, credentials, params.tenantId]);

  async function handleSave(payload: TenantUpdatePayload): Promise<void> {
    if (!credentials) {
      return;
    }
      setSaving(true);
      try {
        const updated = await updateTenant(credentials, params.tenantId, payload);
        setTenant(updated);
      await Promise.all([loadRepositoryOptions(updated), loadAllowlistRequests(updated.tenant_id)]);
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

  async function disconnectGitHubApp() {
    if (!credentials) {
      return;
    }
    if (!window.confirm("Disconnect GitHub App from this tenant?")) {
      return;
    }
    try {
      const updated = await disconnectGitHub(credentials, params.tenantId);
      setTenant(updated);
      setRepositoryOptions([]);
      await loadAllowlistRequests(updated.tenant_id);
      setStatusLine("GitHub disconnected for this tenant.");
    } catch (error) {
      setStatusLine(`Unable to disconnect GitHub: ${(error as Error).message}`);
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

  async function disconnectJiraOauth() {
    if (!credentials) {
      return;
    }
    if (!window.confirm("Disconnect Jira OAuth from this tenant?")) {
      return;
    }
    try {
      const updated = await disconnectJira(credentials, params.tenantId);
      setTenant(updated);
      await loadAllowlistRequests(updated.tenant_id);
      setStatusLine("Jira disconnected for this tenant.");
    } catch (error) {
      setStatusLine(`Unable to disconnect Jira: ${(error as Error).message}`);
    }
  }

  async function approveAllowlistRequest(userId: string) {
    if (!credentials || !tenant) {
      return;
    }
    setApprovingUserId(userId);
    try {
      const result = await approveDiscordAllowlistRequest(credentials, tenant.tenant_id, userId);
      await loadAllowlistRequests(tenant.tenant_id);
      setStatusLine(result.details);
    } catch (error) {
      setStatusLine(`Failed to approve allowlist request: ${(error as Error).message}`);
    } finally {
      setApprovingUserId(null);
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
              <Link href={`/tenants/${encodeURIComponent(tenant.tenant_id)}/webhooks`}>
                <Link2 className="mr-2 h-4 w-4" />
                Webhooks
              </Link>
            </Button>
            <Button asChild variant="outline">
              <Link href="/secrets">
                <KeyRound className="mr-2 h-4 w-4" />
                Manage Secrets
              </Link>
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
        <div className="rounded-md border bg-muted/30 px-3 py-2 text-sm text-foreground">
          {statusLine}
        </div>
        <div className="rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">
          <p className="font-medium text-foreground">Connections</p>
          <p>Configure provider secrets in Secrets Manager, then connect Jira and GitHub below.</p>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          <div className="rounded-md border p-3">
            <p className="text-sm font-medium">Jira Connection</p>
            <p className="mt-1 text-xs text-muted-foreground">
              {tenant.jira.connection_id ? `Connected (${tenant.jira.connection_id})` : "Not connected"}
            </p>
            <div className="mt-3 flex gap-2">
              {tenant.jira.connection_id ? (
                <>
                  <Button variant="outline" disabled>
                    <CheckCircle2 className="mr-2 h-4 w-4" />
                    Connected
                  </Button>
                  <Button variant="outline" onClick={() => void disconnectJiraOauth()}>
                    Disconnect
                  </Button>
                </>
              ) : (
                <Button variant="outline" onClick={() => void connectJira()}>
                  <Link2 className="mr-2 h-4 w-4" />
                  Connect Jira
                </Button>
              )}
            </div>
          </div>
          <div className="rounded-md border p-3">
            <p className="text-sm font-medium">GitHub Connection</p>
            <p className="mt-1 text-xs text-muted-foreground">
              {tenant.github.installation_id
                ? `Connected (installation ${tenant.github.installation_id})`
                : "Not connected"}
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              {tenant.github.installation_id ? (
                <>
                  <Button variant="outline" disabled>
                    <CheckCircle2 className="mr-2 h-4 w-4" />
                    Connected
                  </Button>
                  <Button
                    variant="outline"
                    onClick={() => void refreshRepositoryOptions()}
                    disabled={repositoriesLoading}
                  >
                    <RefreshCw className="mr-2 h-4 w-4" />
                    Refresh Repos
                  </Button>
                  <Button variant="outline" onClick={() => void disconnectGitHubApp()}>
                    Disconnect
                  </Button>
                </>
              ) : (
                <Button variant="outline" onClick={() => void connectGitHubApp()}>
                  <Link2 className="mr-2 h-4 w-4" />
                  Install GitHub App
                </Button>
              )}
            </div>
          </div>
        </div>
        <TenantForm
          mode="edit"
          initialValues={recordToFormValues(tenant)}
          onSubmit={handleSave}
          submitting={saving}
          repositoryOptions={repositoryOptions}
          repositoriesLoading={repositoriesLoading}
        />
        <Card>
          <CardHeader>
            <CardTitle>Discord Access Requests</CardTitle>
            <CardDescription>
              Approve users who requested sensitive Discord command access for this tenant.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {allowlistLoading ? (
              <p className="text-sm text-muted-foreground">Loading requests...</p>
            ) : allowlistRequests.length === 0 ? (
              <p className="text-sm text-muted-foreground">No pending requests.</p>
            ) : (
              <div className="space-y-2">
                {allowlistRequests.map((request) => (
                  <div
                    key={`${request.user_id}-${request.requested_at}`}
                    className="flex flex-wrap items-center justify-between gap-3 rounded-md border p-3"
                  >
                    <div className="space-y-1 text-sm">
                      <p>
                        <strong>User:</strong> {request.user_id}
                      </p>
                      <p className="text-muted-foreground">
                        Requested {new Date(request.requested_at).toLocaleString()}
                        {request.channel_id ? ` from channel ${request.channel_id}` : ""}
                      </p>
                      {request.reason ? (
                        <p className="text-muted-foreground">
                          <strong>Reason:</strong> {request.reason}
                        </p>
                      ) : null}
                    </div>
                    <Button
                      onClick={() => void approveAllowlistRequest(request.user_id)}
                      disabled={approvingUserId === request.user_id}
                    >
                      {approvingUserId === request.user_id ? "Approving..." : "Approve"}
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
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
