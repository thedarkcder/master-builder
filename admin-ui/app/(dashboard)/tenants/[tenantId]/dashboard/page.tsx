"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import {
  Activity,
  BarChart3,
  CheckCircle2,
  Circle,
  ExternalLink,
  RefreshCw,
  Settings2,
  XCircle
} from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { getTenant, listRuns, type RunRecord, type TenantRecord } from "@/lib/api";

type ConnectionIndicatorProps = {
  label: string;
  connected: boolean;
  detail?: string;
};

function ConnectionIndicator({ label, connected, detail }: ConnectionIndicatorProps) {
  const Icon = connected ? CheckCircle2 : XCircle;
  return (
    <div className="flex items-center gap-2.5">
      <Icon className={`h-4 w-4 flex-shrink-0 ${connected ? "text-success" : "text-muted-foreground"}`} />
      <div>
        <p className="text-sm font-medium leading-none">{label}</p>
        {detail ? <p className="mt-0.5 text-xs text-muted-foreground">{detail}</p> : null}
      </div>
    </div>
  );
}

export default function TenantDashboardPage() {
  const params = useParams<{ tenantId: string }>();
  const { credentials, ready } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);

  const [tenant, setTenant] = useState<TenantRecord | null>(null);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  async function loadData() {
    if (!credentials) return;
    setLoading(true);
    setErrorMessage(null);
    try {
      const [tenantPayload, runsPayload] = await Promise.all([
        getTenant(credentials, params.tenantId),
        listRuns(credentials, { tenantId: params.tenantId })
      ]);
      setTenant(tenantPayload);
      const orderedRuns = [...runsPayload].sort((a, b) => b.created_at.localeCompare(a.created_at));
      setRuns(orderedRuns.slice(0, 8));
    } catch (error) {
      setErrorMessage(`Failed to load tenant dashboard: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) void loadData();
  }, [ready, credentials, params.tenantId]);

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold">{tenant?.name ?? tenantId}</h1>
          <p className="text-sm text-muted-foreground">Tenant workspace overview.</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void loadData()} disabled={loading}>
          <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </Button>
      </div>

      {errorMessage ? (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {errorMessage}
        </div>
      ) : null}

      {/* Connection status strip */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wide">
            Connection Status
          </CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="flex gap-8">
              <Skeleton className="h-8 w-32" />
              <Skeleton className="h-8 w-32" />
              <Skeleton className="h-8 w-32" />
            </div>
          ) : (
            <div className="flex flex-wrap gap-8">
              <ConnectionIndicator
                label="Tenant"
                connected={tenant?.is_enabled ?? false}
                detail={tenant?.is_enabled ? "Enabled" : "Disabled"}
              />
              <ConnectionIndicator
                label="Jira"
                connected={Boolean(tenant?.jira.connection_id)}
                detail={tenant?.jira.connection_id ? "Connected" : "Not connected"}
              />
              <ConnectionIndicator
                label="GitHub"
                connected={Boolean(tenant?.github.installation_id)}
                detail={tenant?.github.installation_id ? `Install #${tenant.github.installation_id}` : "Not installed"}
              />
            </div>
          )}
        </CardContent>
      </Card>

      {/* Quick actions */}
      <div className="grid gap-3 sm:grid-cols-3">
        <Link href={`/tenants/${encodeURIComponent(tenantId)}/runs`}>
          <Card className="cursor-pointer transition-shadow hover:shadow-md">
            <CardContent className="flex items-center gap-3 p-4">
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10">
                <Activity className="h-4 w-4 text-primary" />
              </div>
              <div>
                <p className="text-sm font-medium">View Pipeline</p>
                <p className="text-xs text-muted-foreground">All runs & execution logs</p>
              </div>
            </CardContent>
          </Card>
        </Link>
        <Link href={`/tenants/${encodeURIComponent(tenantId)}/analytics/token-overview`}>
          <Card className="cursor-pointer transition-shadow hover:shadow-md">
            <CardContent className="flex items-center gap-3 p-4">
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-info/10">
                <BarChart3 className="h-4 w-4 text-info" />
              </div>
              <div>
                <p className="text-sm font-medium">Analytics</p>
                <p className="text-xs text-muted-foreground">Token usage & trends</p>
              </div>
            </CardContent>
          </Card>
        </Link>
        <Link href={`/tenants/${encodeURIComponent(tenantId)}/edit/integrations`}>
          <Card className="cursor-pointer transition-shadow hover:shadow-md">
            <CardContent className="flex items-center gap-3 p-4">
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-muted">
                <Settings2 className="h-4 w-4 text-muted-foreground" />
              </div>
              <div>
                <p className="text-sm font-medium">Settings</p>
                <p className="text-xs text-muted-foreground">Integrations & config</p>
              </div>
            </CardContent>
          </Card>
        </Link>
      </div>

      {/* Recent runs */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-4">
          <CardTitle className="text-base">Recent Runs</CardTitle>
          <Button asChild variant="ghost" size="sm">
            <Link
              href={`/tenants/${encodeURIComponent(tenantId)}/runs`}
              className="text-xs text-muted-foreground hover:text-foreground"
            >
              View all →
            </Link>
          </Button>
        </CardHeader>
        <CardContent className="p-0">
          {loading ? (
            <div className="space-y-2 px-6 pb-6">
              {[...Array(4)].map((_, i) => <Skeleton key={i} className="h-10 w-full" />)}
            </div>
          ) : runs.length === 0 ? (
            <p className="px-6 pb-6 text-sm text-muted-foreground">No runs found for this tenant.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Run</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>PR</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((run) => (
                  <TableRow key={run.run_id}>
                    <TableCell className="font-medium">
                      <Link
                        className="text-primary hover:underline"
                        href={`/tenants/${encodeURIComponent(tenantId)}/runs/${encodeURIComponent(run.run_id)}`}
                      >
                        {run.issue_key || run.issue_summary?.slice(0, 40) || run.run_id}
                      </Link>
                    </TableCell>
                    <TableCell><StatusBadge status={run.status} /></TableCell>
                    <TableCell>
                      {run.pr_url ? (
                        <Link
                          href={run.pr_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                        >
                          Open PR <ExternalLink className="h-3 w-3" />
                        </Link>
                      ) : (
                        <span className="text-xs text-muted-foreground">—</span>
                      )}
                    </TableCell>
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
