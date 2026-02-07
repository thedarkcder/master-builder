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
  getTenant,
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

  async function loadTenant() {
    if (!credentials) {
      return;
    }
    setLoading(true);
    try {
      const payload = await getTenant(credentials, params.tenantId);
      setTenant(payload);
      setStatusLine(`Loaded ${payload.tenant_id}.`);
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
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <TenantForm mode="edit" initialValues={recordToFormValues(tenant)} onSubmit={handleSave} submitting={saving} />
        <p className="text-sm text-muted-foreground">{statusLine}</p>
      </CardContent>
    </Card>
  );
}
