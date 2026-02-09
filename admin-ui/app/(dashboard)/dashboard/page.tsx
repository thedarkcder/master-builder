"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { listManagedSecrets, listRuns, listTenants, type RunRecord } from "@/lib/api";

type DashboardStats = {
  totalTenants: number;
  enabledTenants: number;
  totalRuns: number;
  runningRuns: number;
  failedRuns: number;
  totalSecrets: number;
};

function statusBadge(status: string) {
  if (status === "succeeded") return <Badge>{status}</Badge>;
  if (status === "failed" || status === "blocked") return <Badge variant="secondary">{status}</Badge>;
  return <Badge variant="outline">{status}</Badge>;
}

export default function DashboardPage() {
  const { credentials, ready } = useAuth();
  const [loading, setLoading] = useState(true);
  const [statusLine, setStatusLine] = useState("Loading analytics...");
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [recentRuns, setRecentRuns] = useState<RunRecord[]>([]);

  async function loadDashboard() {
    if (!credentials) return;
    setLoading(true);
    try {
      const [tenants, runs, secrets] = await Promise.all([
        listTenants(credentials),
        listRuns(credentials, {}),
        listManagedSecrets(credentials)
      ]);

      const enabledTenants = tenants.filter((tenant) => tenant.is_enabled).length;
      const runningRuns = runs.filter((run) => run.status === "queued" || run.status === "running").length;
      const failedRuns = runs.filter((run) => run.status === "failed").length;
      const orderedRuns = [...runs].sort((a, b) => b.created_at.localeCompare(a.created_at));

      setStats({
        totalTenants: tenants.length,
        enabledTenants,
        totalRuns: runs.length,
        runningRuns,
        failedRuns,
        totalSecrets: secrets.length
      });
      setRecentRuns(orderedRuns.slice(0, 6));
      setStatusLine("Analytics updated.");
    } catch (error) {
      setStatusLine(`Failed to load analytics: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) {
      void loadDashboard();
    }
  }, [ready, credentials]);

  const hasError = useMemo(() => statusLine.toLowerCase().includes("failed"), [statusLine]);

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <div>
            <CardTitle>Operations Analytics</CardTitle>
            <CardDescription>Live overview of tenants, runs, and managed secrets.</CardDescription>
          </div>
          <Button variant="outline" onClick={() => void loadDashboard()} disabled={loading}>
            {loading ? "Refreshing..." : "Refresh"}
          </Button>
        </CardHeader>
        <CardContent>
          <p className={hasError ? "text-sm text-red-700" : "text-sm text-muted-foreground"}>{statusLine}</p>
        </CardContent>
      </Card>

      {loading || !stats ? (
        <div className="grid gap-3 md:grid-cols-3">
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-28 w-full" />
        </div>
      ) : (
        <div className="grid gap-3 md:grid-cols-3">
          <Card>
            <CardHeader>
              <CardDescription>Tenants</CardDescription>
              <CardTitle>{stats.totalTenants}</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">{stats.enabledTenants} enabled</CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>Runs</CardDescription>
              <CardTitle>{stats.totalRuns}</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">{stats.runningRuns} active now</CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>Failed Runs</CardDescription>
              <CardTitle>{stats.failedRuns}</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">Needs investigation</CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>Managed Secrets</CardDescription>
              <CardTitle>{stats.totalSecrets}</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">Configured in secret store</CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>Next Action</CardDescription>
              <CardTitle className="text-base">Select Tenant</CardTitle>
            </CardHeader>
            <CardContent>
              <Button asChild size="sm" variant="outline">
                <Link href="/tenants/select">Open selector</Link>
              </Button>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>Onboarding</CardDescription>
              <CardTitle className="text-base">Create Tenant</CardTitle>
            </CardHeader>
            <CardContent>
              <Button asChild size="sm" variant="outline">
                <Link href="/tenants/new">Start onboarding</Link>
              </Button>
            </CardContent>
          </Card>
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Recent Runs</CardTitle>
          <CardDescription>Most recently created orchestration runs.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          {loading ? (
            <div className="space-y-2">
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </div>
          ) : recentRuns.length === 0 ? (
            <p className="text-sm text-muted-foreground">No runs recorded yet.</p>
          ) : (
            recentRuns.map((run) => (
              <div key={run.run_id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3">
                <div>
                  <p className="text-sm font-medium">{run.issue_key || run.run_id}</p>
                  <p className="text-xs text-muted-foreground">
                    {run.tenant_id} • {new Date(run.created_at).toLocaleString()}
                  </p>
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
