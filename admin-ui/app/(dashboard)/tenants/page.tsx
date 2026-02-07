"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { CheckCircle2, CircleOff, RefreshCw } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from "@/components/ui/table";
import {
  deleteTenant,
  listTenants,
  testGithub,
  testJira,
  type TenantRecord
} from "@/lib/api";

export default function TenantsPage() {
  const { credentials, ready } = useAuth();
  const [tenants, setTenants] = useState<TenantRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [statusLine, setStatusLine] = useState("Load tenants to begin.");

  async function loadTenants() {
    if (!credentials) {
      return;
    }
    setLoading(true);
    try {
      const payload = await listTenants(credentials);
      setTenants(payload);
      setStatusLine(`Loaded ${payload.length} tenant(s).`);
    } catch (error) {
      setStatusLine(`Failed to load tenants: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) {
      void loadTenants();
    }
  }, [ready, credentials]);

  async function runHealthChecks(tenantId: string) {
    if (!credentials) {
      return;
    }
    try {
      const jira = await testJira(credentials, tenantId);
      const github = await testGithub(credentials, tenantId);
      setStatusLine(
        `${tenantId}: Jira ${jira.ok ? "ok" : "fail"} (${jira.details}); GitHub ${github.ok ? "ok" : "fail"} (${github.details})`
      );
    } catch (error) {
      setStatusLine(`Health check failed: ${(error as Error).message}`);
    }
  }

  async function handleDelete(tenantId: string) {
    if (!credentials) {
      return;
    }
    try {
      await deleteTenant(credentials, tenantId);
      setStatusLine(`Deleted ${tenantId}.`);
      await loadTenants();
    } catch (error) {
      setStatusLine(`Delete failed: ${(error as Error).message}`);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle>Tenants</CardTitle>
            <CardDescription>Manage tenant records and run integration checks.</CardDescription>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => void loadTenants()}>
              <RefreshCw className="mr-2 h-4 w-4" />
              Refresh
            </Button>
            <Button asChild>
              <Link href="/tenants/new">Create Tenant</Link>
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Tenant</TableHead>
              <TableHead>Name</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Updated</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {tenants.map((tenant) => (
              <TableRow key={tenant.tenant_id}>
                <TableCell className="font-medium">{tenant.tenant_id}</TableCell>
                <TableCell>{tenant.name}</TableCell>
                <TableCell>
                  {tenant.is_enabled ? (
                    <span className="inline-flex items-center gap-1 text-xs text-emerald-700">
                      <CheckCircle2 className="h-3 w-3" /> Enabled
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-xs text-rose-700">
                      <CircleOff className="h-3 w-3" /> Disabled
                    </span>
                  )}
                </TableCell>
                <TableCell>{new Date(tenant.updated_at).toLocaleString()}</TableCell>
                <TableCell>
                  <div className="flex flex-wrap gap-1">
                    <Button size="sm" variant="outline" asChild>
                      <Link href={`/tenants/${tenant.tenant_id}/edit`}>Edit</Link>
                    </Button>
                    <Button size="sm" variant="secondary" onClick={() => void runHealthChecks(tenant.tenant_id)}>
                      Health
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => void handleDelete(tenant.tenant_id)}>
                      Delete
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        {!tenants.length && !loading ? (
          <p className="rounded-md border border-dashed px-3 py-5 text-sm text-muted-foreground">
            No tenants found yet. Create one to start onboarding workflows.
          </p>
        ) : null}

        <p className="text-sm text-muted-foreground">{statusLine}</p>
      </CardContent>
    </Card>
  );
}
