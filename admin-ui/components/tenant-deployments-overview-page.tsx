"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { AlertCircle, ExternalLink, RefreshCw, Server } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { getTenantDeploymentsOverview, type TenantDeploymentsOverviewRecord } from "@/lib/api";
import { cn } from "@/lib/utils";

function formatTimestamp(value: string | null): string {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function formatConfidence(value: number | null): string {
  if (value == null || Number.isNaN(value)) {
    return "—";
  }
  const scaled = value > 1 ? value : value * 100;
  return `${Math.round(scaled)}%`;
}

function appStatusVariant(status: string): "default" | "secondary" | "outline" | "success" | "warning" | "destructive" | "info" {
  const normalized = status.trim().toLowerCase();
  if (normalized === "live") return "success";
  if (normalized === "deploying") return "warning";
  if (normalized === "ready") return "info";
  if (normalized === "needs_pr_merge") return "warning";
  if (normalized === "failed") return "destructive";
  return "outline";
}

function buildStrategyLabel(strategy: string | null | undefined): string {
  const normalized = String(strategy || "").trim().toLowerCase();
  if (normalized === "docker_compose") return "Docker Compose";
  if (normalized === "dockerfile") return "Dockerfile";
  if (normalized === "nixpacks") return "Nixpacks";
  return normalized || "—";
}

export function TenantDeploymentsOverviewPage() {
  const params = useParams<{ tenantId: string }>();
  const { credentials, ready } = useAuth();
  const [overview, setOverview] = useState<TenantDeploymentsOverviewRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("");

  const sortedApps = useMemo(
    () => [...(overview?.apps ?? [])].sort((left, right) => right.updated_at.localeCompare(left.updated_at)),
    [overview?.apps],
  );

  async function loadOverview({ silent = false }: { silent?: boolean } = {}) {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      const payload = await getTenantDeploymentsOverview(credentials, params.tenantId);
      setOverview(payload);
      if (!silent) {
        setStatusLine(`Loaded ${payload.summary.total_apps} app${payload.summary.total_apps === 1 ? "" : "s"}.`);
      }
    } catch (error) {
      setStatusLine(`Failed to load managed deployments overview: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) {
      void loadOverview({ silent: true });
    }
  }, [ready, credentials, params.tenantId]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-end gap-2">
        <Button variant="outline" size="sm" onClick={() => void loadOverview()} disabled={busy}>
          <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
          Refresh
        </Button>
      </div>

      {statusLine ? (
        <div className="rounded-xl border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">{statusLine}</div>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {[
          { label: "Apps", value: overview?.summary.total_apps ?? 0, note: "Across projects" },
          { label: "Live", value: overview?.summary.live_count ?? 0, note: "Healthy and deployed" },
          { label: "Deploying", value: overview?.summary.deploying_count ?? 0, note: "In progress" },
          { label: "Needs PR", value: overview?.summary.needs_pr_merge_count ?? 0, note: "Waiting on merge" },
          { label: "Failed", value: overview?.summary.failed_count ?? 0, note: "Needs attention" },
        ].map((item) => (
          <Card key={item.label}>
            <CardContent className="px-4 py-3">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">{item.label}</p>
              <p className="mt-1 text-2xl font-semibold">{item.value}</p>
              <p className="mt-1 text-xs text-muted-foreground">{item.note}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Latest failures</CardTitle>
          <CardDescription>Managed deployment items that most recently failed or need attention.</CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {(overview?.latest_failures ?? []).length === 0 ? (
            <div className="px-6 py-10 text-center text-sm text-muted-foreground">
              No failures reported in the current overview.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Project</TableHead>
                  <TableHead>App</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Failure</TableHead>
                  <TableHead>Updated</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {overview?.latest_failures?.map((failure) => (
                  <TableRow key={`${failure.project_id}-${failure.app_id}-${failure.updated_at}`}>
                    <TableCell>
                      <Link
                        href={`/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(failure.project_id)}/apps`}
                        className="inline-flex items-center gap-1 text-primary hover:underline"
                      >
                        {failure.project_name}
                        <ExternalLink className="h-3 w-3" />
                      </Link>
                    </TableCell>
                    <TableCell>
                      <div className="space-y-1">
                        <p className="font-medium">{failure.app_name}</p>
                        <p className="font-mono text-xs text-muted-foreground">{failure.source_path ?? failure.slug}</p>
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge variant={appStatusVariant(failure.status)}>{failure.status}</Badge>
                    </TableCell>
                    <TableCell className="max-w-[360px]">
                      {failure.last_error ? (
                        <div className="flex items-start gap-2">
                          <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0 text-destructive" />
                          <p className="line-clamp-2 text-sm text-destructive" title={failure.last_error}>
                            {failure.last_error}
                          </p>
                        </div>
                      ) : (
                        <span className="text-sm text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">{formatTimestamp(failure.updated_at)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">All apps</CardTitle>
          <CardDescription>Cross-project managed deployment status across the tenant.</CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {sortedApps.length === 0 ? (
            <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
              <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted">
                <Server className="h-6 w-6 text-muted-foreground" />
              </div>
              <p className="mt-4 text-sm font-medium">No deployed apps yet</p>
              <p className="mt-1 text-sm text-muted-foreground">
                Run Analyze Repo from a project to create app candidates and managed deployment surfaces.
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Project</TableHead>
                  <TableHead>App</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Runtime</TableHead>
                  <TableHead>Build</TableHead>
                  <TableHead>Confidence</TableHead>
                  <TableHead>Updated</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sortedApps.map((app) => (
                  <TableRow key={app.app_id} className={cn(String(app.status || "").toLowerCase() === "failed" && "border-l-2 border-l-destructive")}>
                    <TableCell>
                      <Link
                        href={`/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(app.project_id)}/apps`}
                        className="inline-flex items-center gap-1 text-primary hover:underline"
                      >
                        {app.project_name}
                        <ExternalLink className="h-3 w-3" />
                      </Link>
                    </TableCell>
                    <TableCell>
                      <div className="space-y-1">
                        <p className="font-medium">{app.app_name}</p>
                        <p className="font-mono text-xs text-muted-foreground">{app.source_path ?? app.slug}</p>
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge variant={appStatusVariant(app.status)}>{app.status}</Badge>
                      {app.last_error ? <p className="mt-1 max-w-[220px] truncate text-xs text-muted-foreground" title={app.last_error}>{app.last_error}</p> : null}
                    </TableCell>
                    <TableCell>
                      <div className="space-y-0.5 text-sm">
                        <p>{app.detected_runtime ?? "—"}</p>
                        <p className="text-xs text-muted-foreground">{app.detected_language ?? "—"}</p>
                      </div>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">{buildStrategyLabel(app.build_strategy)}</TableCell>
                    <TableCell className="text-sm text-muted-foreground">{formatConfidence(app.detection_confidence)}</TableCell>
                    <TableCell className="text-sm text-muted-foreground">{formatTimestamp(app.updated_at)}</TableCell>
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
