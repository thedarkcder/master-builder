"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertCircle,
  Building2,
  KeyRound,
  Plus,
  RefreshCw
} from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { getDefaultAuthenticatedRoute } from "@/lib/auth-routing";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { listManagedSecrets, listRuns, listTenants, type RunRecord } from "@/lib/api";

type DashboardStats = {
  totalTenants: number;
  enabledTenants: number;
  totalRuns: number;
  runningRuns: number;
  failedRuns: number;
  totalSecrets: number;
};

type StatCardProps = {
  label: string;
  value: string | number;
  sub: string;
  icon: React.ReactNode;
  iconBg: string;
};

function StatCard({ label, value, sub, icon, iconBg }: StatCardProps) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between space-y-0 pb-2">
        <p className="text-sm font-medium text-muted-foreground">{label}</p>
        <div className={`flex h-8 w-8 items-center justify-center rounded-lg ${iconBg}`}>{icon}</div>
      </CardHeader>
      <CardContent>
        <p className="text-3xl font-bold tracking-tight">{value}</p>
        <p className="mt-1 text-xs text-muted-foreground">{sub}</p>
      </CardContent>
    </Card>
  );
}

export default function DashboardPage() {
  const { credentials, ready, principal } = useAuth();
  const [loading, setLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [recentRuns, setRecentRuns] = useState<RunRecord[]>([]);

  async function loadDashboard() {
    if (!credentials) return;
    setLoading(true);
    setErrorMessage(null);
    try {
      const [tenants, runs, secrets] = await Promise.all([
        listTenants(credentials),
        listRuns(credentials, {}),
        listManagedSecrets(credentials)
      ]);

      const enabledTenants = tenants.filter((t) => t.is_enabled).length;
      const runningRuns = runs.filter((r) => r.status === "queued" || r.status === "running").length;
      const failedRuns = runs.filter((r) => r.status === "failed").length;
      const orderedRuns = [...runs].sort((a, b) => b.created_at.localeCompare(a.created_at));

      setStats({ totalTenants: tenants.length, enabledTenants, totalRuns: runs.length, runningRuns, failedRuns, totalSecrets: secrets.length });
      setRecentRuns(orderedRuns.slice(0, 8));
    } catch (error) {
      setErrorMessage(`Failed to load dashboard: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (ready && credentials && principal?.principal_type === "tenant_user") {
      window.location.replace(getDefaultAuthenticatedRoute(principal));
      return;
    }
  }, [credentials, principal, ready]);

  useEffect(() => {
    if (ready && credentials) void loadDashboard();
  }, [ready, credentials, principal?.principal_type]);

  if (principal?.principal_type === "tenant_user") {
    return <main className="p-8 text-sm text-muted-foreground">Redirecting to workspace...</main>;
  }

  const statsData = useMemo(() => {
    if (!stats) return [];
    return [
      {
        label: "Total Runs",
        value: stats.totalRuns,
        sub: `${stats.runningRuns} active right now`,
        icon: <Activity className="h-4 w-4 text-success" />,
        iconBg: "bg-success/10"
      },
      {
        label: "Active Runs",
        value: stats.runningRuns,
        sub: "Queued or currently running",
        icon: <Activity className="h-4 w-4 text-info" />,
        iconBg: "bg-info/10"
      },
      {
        label: "Failed Runs",
        value: stats.failedRuns,
        sub: "Require investigation",
        icon: <AlertCircle className="h-4 w-4 text-destructive" />,
        iconBg: "bg-destructive/10"
      },
      {
        label: "Tenants",
        value: stats.totalTenants,
        sub: `${stats.enabledTenants} enabled`,
        icon: <Building2 className="h-4 w-4 text-primary" />,
        iconBg: "bg-primary/10"
      },
      {
        label: "Managed Secrets",
        value: stats.totalSecrets,
        sub: "Configured in secret store",
        icon: <KeyRound className="h-4 w-4 text-warning" />,
        iconBg: "bg-warning/10"
      }
    ];
  }, [stats]);

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold">Operations Overview</h1>
          <p className="text-sm text-muted-foreground">Live view of tenants, runs, and managed secrets.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button asChild variant="outline" size="sm">
            <Link href="/tenants/new">
              <Plus className="mr-1.5 h-3.5 w-3.5" />
              New Tenant
            </Link>
          </Button>
          <Button variant="outline" size="sm" onClick={() => void loadDashboard()} disabled={loading}>
            <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </Button>
        </div>
      </div>

      {errorMessage ? (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-5 py-4 text-sm leading-relaxed text-destructive break-words">
          {errorMessage}
        </div>
      ) : null}

      {/* Stat cards */}
      {loading || !stats ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
          {[...Array(5)].map((_, i) => <Skeleton key={i} className="h-28 w-full rounded-lg" />)}
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
          {statsData.map((s) => (
            <StatCard key={s.label} {...s} />
          ))}
        </div>
      )}

      {/* Recent runs */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-4">
          <CardTitle className="text-base">Recent Runs</CardTitle>
          <Button asChild variant="ghost" size="sm">
            <Link href="/tenants/select" className="text-xs text-muted-foreground hover:text-foreground">
              View all →
            </Link>
          </Button>
        </CardHeader>
        <CardContent className="p-0">
          {loading ? (
            <div className="space-y-2 px-6 pb-6">
              {[...Array(4)].map((_, i) => <Skeleton key={i} className="h-10 w-full" />)}
            </div>
          ) : recentRuns.length === 0 ? (
            <p className="px-6 pb-6 text-sm text-muted-foreground">No runs recorded yet.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Run</TableHead>
                  <TableHead>Tenant</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {recentRuns.map((run) => (
                  <TableRow key={run.run_id}>
                    <TableCell className="font-medium">
                      <Link
                        className="text-primary hover:underline"
                        href={`/runs/${encodeURIComponent(run.run_id)}`}
                      >
                        {run.issue_key || run.run_id}
                      </Link>
                    </TableCell>
                    <TableCell className="text-muted-foreground text-sm">{run.tenant_id}</TableCell>
                    <TableCell><StatusBadge status={run.status} /></TableCell>
                    <TableCell className="text-muted-foreground text-sm">
                      {new Date(run.created_at).toLocaleString()}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
