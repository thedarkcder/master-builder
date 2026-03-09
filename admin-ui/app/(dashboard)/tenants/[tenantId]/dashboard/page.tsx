"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { getTenant, listRuns, type RunRecord, type TenantRecord } from "@/lib/api";

function statusBadge(status: string) {
  if (status === "succeeded") return <Badge>{status}</Badge>;
  if (status === "failed" || status === "blocked") return <Badge variant="secondary">{status}</Badge>;
  return <Badge variant="outline">{status}</Badge>;
}

export default function TenantDashboardPage() {
  const params = useParams<{ tenantId: string }>();
  const { credentials, ready } = useAuth();

  const [tenant, setTenant] = useState<TenantRecord | null>(null);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusLine, setStatusLine] = useState("");

  async function loadData() {
    if (!credentials) return;
    setLoading(true);
    try {
      const [tenantPayload, runsPayload] = await Promise.all([
        getTenant(credentials, params.tenantId),
        listRuns(credentials, { tenantId: params.tenantId })
      ]);
      setTenant(tenantPayload);
      const orderedRuns = [...runsPayload].sort((a, b) => b.created_at.localeCompare(a.created_at));
      setRuns(orderedRuns.slice(0, 8));
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to load tenant dashboard: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) {
      void loadData();
    }
  }, [ready, credentials, params.tenantId]);

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <div>
            <CardTitle>{tenant?.name ?? params.tenantId}</CardTitle>
            <CardDescription>Tenant workspace overview.</CardDescription>
          </div>
          <Button variant="outline" onClick={() => void loadData()} disabled={loading}>
            {loading ? "Refreshing..." : "Refresh"}
          </Button>
        </CardHeader>
        {statusLine ? <CardContent className="text-sm text-red-700">{statusLine}</CardContent> : null}
      </Card>

      {loading ? (
        <div className="grid gap-3 md:grid-cols-3">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : (
        <div className="grid gap-3 md:grid-cols-3">
          <Card>
            <CardHeader>
              <CardDescription>Tenant Status</CardDescription>
              <CardTitle>{tenant?.is_enabled ? "Enabled" : "Disabled"}</CardTitle>
            </CardHeader>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>Jira</CardDescription>
              <CardTitle>{tenant?.jira.connection_id ? "Connected" : "Not Connected"}</CardTitle>
            </CardHeader>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>GitHub App</CardDescription>
              <CardTitle>{tenant?.github.installation_id ? "Installed" : "Not Installed"}</CardTitle>
            </CardHeader>
          </Card>
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Recent Runs</CardTitle>
          <CardDescription>Latest runs for this tenant.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          {loading ? (
            <>
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </>
          ) : runs.length === 0 ? (
            <p className="text-sm text-muted-foreground">No runs found for this tenant.</p>
          ) : (
            runs.map((run) => (
              <div key={run.run_id} className="flex items-center justify-between rounded-md border p-3">
                <div>
                  <p className="text-sm font-medium">
                    <Link
                      className="text-primary hover:underline"
                      href={`/tenants/${encodeURIComponent(params.tenantId)}/runs/${encodeURIComponent(run.run_id)}`}
                    >
                      {run.issue_key || run.run_id}
                    </Link>
                  </p>
                  <p className="text-xs text-muted-foreground">{new Date(run.created_at).toLocaleString()}</p>
                </div>
                {statusBadge(run.status)}
              </div>
            ))
          )}
        </CardContent>
      </Card>
    </div>
  );
}
