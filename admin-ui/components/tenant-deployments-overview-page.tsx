"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { AlertCircle, Copy, ExternalLink, Server } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import {
  getTenantDeploymentPlane,
  getTenantDeploymentsOverview,
  createDeploymentHost,
  listDeploymentHosts,
  updateTenantDeploymentPlane,
  type DeploymentHostBootstrapRecord,
  type DeploymentHostCreatePayload,
  type DeploymentHostRecord,
  type TenantDeploymentPlaneRecord,
  type TenantDeploymentsOverviewRecord,
} from "@/lib/api/deployments";
import { canAccessPlatformAdmin } from "@/lib/auth-routing";
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

function hostStateVariant(status: string): "default" | "secondary" | "outline" | "success" | "warning" | "destructive" | "info" {
  const normalized = status.trim().toLowerCase();
  if (normalized === "active") return "success";
  if (normalized === "degraded") return "warning";
  if (normalized === "offline") return "destructive";
  if (normalized === "provisioning") return "info";
  if (normalized === "retired") return "secondary";
  return "outline";
}

function formatHostMetadata(metadata: Record<string, unknown>): string {
  const entries = Object.entries(metadata || {});
  if (entries.length === 0) {
    return "—";
  }
  const preview = entries.slice(0, 4).map(([key, value]) => `${key}: ${formatHostValue(value)}`);
  return entries.length > 4 ? `${preview.join(" · ")} · +${entries.length - 4} more` : preview.join(" · ");
}

function formatHostValue(value: unknown): string {
  if (value == null) {
    return "null";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return "[unserializable]";
  }
}

function parseCapabilityList(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)
    .filter((item, index, items) => items.indexOf(item) === index);
}

type ManagedHostDraft = {
  label: string;
  infrastructureProvider: "" | "aws" | "hetzner";
  region: string;
  capabilitiesText: string;
};

function emptyManagedHostDraft(): ManagedHostDraft {
  return {
    label: "",
    infrastructureProvider: "hetzner",
    region: "",
    capabilitiesText: "",
  };
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
  const { credentials, principal, principalReady, ready } = useAuth();
  const [overview, setOverview] = useState<TenantDeploymentsOverviewRecord | null>(null);
  const [deploymentPlane, setDeploymentPlane] = useState<TenantDeploymentPlaneRecord | null>(null);
  const [hosts, setHosts] = useState<DeploymentHostRecord[]>([]);
  const [hostBootstrap, setHostBootstrap] = useState<DeploymentHostBootstrapRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [hostBusy, setHostBusy] = useState(false);
  const [hostStatusLine, setHostStatusLine] = useState("");
  const [hostDraft, setHostDraft] = useState<ManagedHostDraft>(() => emptyManagedHostDraft());
  const [selectedHostId, setSelectedHostId] = useState("");
  const isPlatformAdmin = canAccessPlatformAdmin(principal);

  const sortedApps = useMemo(
    () => [...(overview?.apps ?? [])].sort((left, right) => right.updated_at.localeCompare(left.updated_at)),
    [overview?.apps],
  );

  const sortedHosts = useMemo(
    () => [...hosts].sort((left, right) => right.updated_at.localeCompare(left.updated_at)),
    [hosts],
  );

  async function loadOverview({ silent = false }: { silent?: boolean } = {}) {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      const [overviewResult, hostsResult, planeResult] = await Promise.allSettled([
        getTenantDeploymentsOverview(credentials, params.tenantId),
        isPlatformAdmin ? listDeploymentHosts(credentials) : Promise.resolve([] as DeploymentHostRecord[]),
        isPlatformAdmin ? getTenantDeploymentPlane(credentials, params.tenantId) : Promise.resolve(null),
      ]);

      if (overviewResult.status === "fulfilled") {
        const payload = overviewResult.value;
        setOverview(payload);
        if (!silent) {
          setStatusLine(`Loaded ${payload.summary.total_apps} app${payload.summary.total_apps === 1 ? "" : "s"}.`);
        }
      } else if (!silent) {
        setStatusLine(`Failed to load managed deployments overview: ${overviewResult.reason instanceof Error ? overviewResult.reason.message : String(overviewResult.reason)}`);
      }

      if (hostsResult.status === "fulfilled") {
        setHosts(hostsResult.value);
        if (!silent) {
          setHostStatusLine(`Loaded ${hostsResult.value.length} managed host${hostsResult.value.length === 1 ? "" : "s"}.`);
        }
      } else {
        setHostStatusLine(
          `Failed to load managed hosts: ${hostsResult.reason instanceof Error ? hostsResult.reason.message : String(hostsResult.reason)}`,
        );
      }
      if (planeResult.status === "fulfilled") {
        setDeploymentPlane(planeResult.value);
        setSelectedHostId(planeResult.value?.managed_host_id ?? "");
      } else if (isPlatformAdmin) {
        setHostStatusLine(
          `Failed to load tenant deployment plane: ${planeResult.reason instanceof Error ? planeResult.reason.message : String(planeResult.reason)}`,
        );
      }
    } catch (error) {
      setStatusLine(`Failed to load managed deployments overview: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleCreateHost() {
    if (!credentials || !isPlatformAdmin) {
      return;
    }
    const label = hostDraft.label.trim();
    if (!label) {
      setHostStatusLine("Enter a host label before creating a managed host.");
      return;
    }
    setHostBusy(true);
    try {
      const payload: DeploymentHostCreatePayload = {
        label,
        infrastructure_provider: hostDraft.infrastructureProvider || null,
        region: hostDraft.region.trim() || null,
        capabilities: parseCapabilityList(hostDraft.capabilitiesText),
      };
      const created = await createDeploymentHost(credentials, payload);
      setHosts((current) => [created.host, ...current.filter((host) => host.host_id !== created.host.host_id)]);
      setHostBootstrap(created);
      setHostStatusLine(`Created managed host ${created.host.label}. Recovery token is ready for manual agent bootstrap.`);
      setHostDraft(emptyManagedHostDraft());
    } catch (error) {
      setHostStatusLine(`Failed to create managed host: ${(error as Error).message}`);
    } finally {
      setHostBusy(false);
    }
  }

  async function copyBootstrapToken(token: string) {
    try {
      await navigator.clipboard.writeText(token);
      setHostStatusLine("Bootstrap token copied.");
    } catch (error) {
      setHostStatusLine(`Copy failed: ${(error as Error).message}`);
    }
  }

  async function handleAssignManagedHost() {
    if (!credentials || !isPlatformAdmin || !deploymentPlane) {
      return;
    }
    setHostBusy(true);
    try {
      const updated = await updateTenantDeploymentPlane(credentials, params.tenantId, {
        ...deploymentPlane,
        managed_host_id: selectedHostId || null,
      });
      setDeploymentPlane(updated);
      setSelectedHostId(updated.managed_host_id ?? "");
      setHostStatusLine(
        updated.managed_host_id
          ? "Managed host assigned to the tenant deployment plane."
          : "Managed host assignment cleared from the tenant deployment plane.",
      );
    } catch (error) {
      setHostStatusLine(`Failed to update tenant managed host: ${(error as Error).message}`);
    } finally {
      setHostBusy(false);
    }
  }

  const selectedHost = useMemo(
    () => sortedHosts.find((host) => host.host_id === selectedHostId) ?? null,
    [selectedHostId, sortedHosts],
  );

  useEffect(() => {
    if (ready && principalReady && credentials) {
      void loadOverview({ silent: true });
    }
  }, [ready, principalReady, credentials, params.tenantId, isPlatformAdmin]);

  return (
    <div className="space-y-6">
      {isPlatformAdmin ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Managed hosts</CardTitle>
            <CardDescription>Compose-managed stacks self-bootstrap these hosts. Manual host creation remains available for advanced environments.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="rounded-xl border bg-muted/10 p-4">
              <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_auto] xl:items-end">
                <div className="space-y-3">
                  <div>
                    <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Tenant host assignment</p>
                    <p className="mt-1 text-sm text-muted-foreground">
                      Choose which managed host executes restore commands for this tenant.
                    </p>
                  </div>
                  <div className="grid gap-3 md:grid-cols-2">
                    <div className="space-y-2">
                      <label className="text-sm font-medium" htmlFor="tenant-managed-host">
                        Assigned host
                      </label>
                      <select
                        id="tenant-managed-host"
                        value={selectedHostId}
                        onChange={(event) => setSelectedHostId(event.target.value)}
                        className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                      >
                        <option value="">No host assigned</option>
                        {sortedHosts.map((host) => (
                          <option key={host.host_id} value={host.host_id}>
                            {host.label} {host.region ? `(${host.region})` : ""}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="space-y-2">
                      <p className="text-sm font-medium">Current plane state</p>
                      <div className="flex min-h-10 items-center gap-2 rounded-md border bg-background px-3 text-sm">
                        <Badge variant={hostStateVariant(deploymentPlane?.state ?? "outline")}>
                          {deploymentPlane?.state ?? "unconfigured"}
                        </Badge>
                        <span className="text-muted-foreground">
                          {deploymentPlane?.managed_host_id ?? "No host attached"}
                        </span>
                      </div>
                    </div>
                  </div>
                  {selectedHost ? (
                    <div className="grid gap-2 text-sm text-muted-foreground md:grid-cols-3">
                      <p>State: <span className="font-medium text-foreground">{selectedHost.state}</span></p>
                      <p>Agent: <span className="font-medium text-foreground">{selectedHost.agent_version ?? "—"}</span></p>
                      <p>Last seen: <span className="font-medium text-foreground">{formatTimestamp(selectedHost.last_seen_at)}</span></p>
                    </div>
                  ) : null}
                </div>
                <Button
                  type="button"
                  variant="outline"
                  disabled={hostBusy || !deploymentPlane}
                  onClick={() => void handleAssignManagedHost()}
                >
                  {hostBusy ? "Saving..." : "Save host assignment"}
                </Button>
              </div>
            </div>

            <div className="grid gap-6 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)]">
              <form
                className="space-y-4"
                onSubmit={(event) => {
                  event.preventDefault();
                  void handleCreateHost();
                }}
              >
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="space-y-2 md:col-span-2">
                    <label className="text-sm font-medium" htmlFor="managed-host-label">
                      Host label
                    </label>
                    <Input
                      id="managed-host-label"
                      value={hostDraft.label}
                      onChange={(event) => setHostDraft((current) => ({ ...current, label: event.target.value }))}
                      placeholder="Primary managed host"
                    />
                  </div>
                  <div className="space-y-2">
                    <label className="text-sm font-medium" htmlFor="managed-host-provider">
                      Infrastructure
                    </label>
                    <select
                      id="managed-host-provider"
                      value={hostDraft.infrastructureProvider}
                      onChange={(event) =>
                        setHostDraft((current) => ({
                          ...current,
                          infrastructureProvider: event.target.value as "" | "aws" | "hetzner",
                        }))
                      }
                      className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                    >
                      <option value="hetzner">Hetzner</option>
                      <option value="aws">AWS</option>
                    </select>
                  </div>
                  <div className="space-y-2">
                    <label className="text-sm font-medium" htmlFor="managed-host-region">
                      Region
                    </label>
                    <Input
                      id="managed-host-region"
                      value={hostDraft.region}
                      onChange={(event) => setHostDraft((current) => ({ ...current, region: event.target.value }))}
                      placeholder="eu-west-1"
                    />
                  </div>
                  <div className="space-y-2 md:col-span-2">
                    <label className="text-sm font-medium" htmlFor="managed-host-capabilities">
                      Capabilities
                    </label>
                    <Textarea
                      id="managed-host-capabilities"
                      value={hostDraft.capabilitiesText}
                      onChange={(event) => setHostDraft((current) => ({ ...current, capabilitiesText: event.target.value }))}
                      placeholder="restore_database&#10;postgres&#10;mysql"
                      className="min-h-[96px]"
                    />
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <Button type="submit" disabled={hostBusy}>
                    {hostBusy ? "Creating..." : "Create managed host"}
                  </Button>
                  <p className="text-xs text-muted-foreground">Use this only when you need to bootstrap a host outside the managed stack.</p>
                </div>
              </form>

              <div className="space-y-3">
                <div className="rounded-xl border bg-muted/20 p-4">
                  <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">What the host records</p>
                  <ul className="mt-3 space-y-2 text-sm text-muted-foreground">
                    <li>Label, region, capabilities, and lifecycle state.</li>
                    <li>Last seen time and agent version after registration.</li>
                    <li>Bootstrap recovery token for manual agent registration and re-registration flows.</li>
                  </ul>
                </div>

                {hostBootstrap ? (
                  <div className="rounded-xl border border-dashed border-primary/40 bg-primary/5 p-4">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Bootstrap token</p>
                        <p className="mt-1 text-sm font-medium">{hostBootstrap.host.label}</p>
                        <p className="mt-0.5 text-xs text-muted-foreground">
                          Host ID {hostBootstrap.host.host_id} · {hostBootstrap.host.state}
                        </p>
                      </div>
                      <Button variant="outline" size="sm" onClick={() => void copyBootstrapToken(hostBootstrap.bootstrap_token)}>
                        <Copy className="mr-1.5 h-3.5 w-3.5" />
                        Copy
                      </Button>
                    </div>
                    <div className="mt-3 rounded-md border bg-background px-3 py-2 font-mono text-xs break-all">
                      {hostBootstrap.bootstrap_token}
                    </div>
                    <div className="mt-3 grid gap-2 text-sm text-muted-foreground">
                      <p>Region: {hostBootstrap.host.region ?? "—"}</p>
                      <p>Capabilities: {hostBootstrap.host.capabilities.length > 0 ? hostBootstrap.host.capabilities.join(", ") : "—"}</p>
                      <p>Metadata: {formatHostMetadata(hostBootstrap.host.metadata)}</p>
                    </div>
                  </div>
                ) : (
                  <div className="rounded-xl border bg-muted/10 p-4 text-sm text-muted-foreground">
                    Managed stacks bootstrap hosts automatically. Create one here only for manual or external host registration.
                  </div>
                )}
              </div>
            </div>

            {hostStatusLine ? (
              <div className="rounded-xl border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">{hostStatusLine}</div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {isPlatformAdmin ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Host inventory</CardTitle>
            <CardDescription>Lifecycle, freshness, and metadata for managed hosts.</CardDescription>
          </CardHeader>
          <CardContent className="p-0">
            {sortedHosts.length === 0 ? (
              <div className="px-6 py-10 text-center text-sm text-muted-foreground">
                No managed hosts have been created yet.
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Host</TableHead>
                    <TableHead>State</TableHead>
                    <TableHead>Region</TableHead>
                    <TableHead>Capabilities</TableHead>
                    <TableHead>Agent</TableHead>
                    <TableHead>Last seen</TableHead>
                    <TableHead>Metadata</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {sortedHosts.map((host) => (
                    <TableRow key={host.host_id} className={cn(host.state === "offline" && "border-l-2 border-l-destructive")}>
                      <TableCell>
                        <div className="space-y-1">
                          <p className="font-medium">{host.label}</p>
                          <p className="font-mono text-xs text-muted-foreground">{host.host_id}</p>
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant={hostStateVariant(host.state)}>{host.state}</Badge>
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">{host.region ?? "—"}</TableCell>
                      <TableCell>
                        {host.capabilities.length > 0 ? (
                          <div className="flex flex-wrap gap-1">
                            {host.capabilities.map((capability) => (
                              <Badge key={`${host.host_id}-${capability}`} variant="outline">
                                {capability}
                              </Badge>
                            ))}
                          </div>
                        ) : (
                          <span className="text-sm text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">{host.agent_version ?? "—"}</TableCell>
                      <TableCell className="text-sm text-muted-foreground">{formatTimestamp(host.last_seen_at)}</TableCell>
                      <TableCell className="max-w-[320px] text-sm text-muted-foreground">
                        <span title={formatHostMetadata(host.metadata)}>{formatHostMetadata(host.metadata)}</span>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      ) : null}

      {statusLine ? (
        <div className="rounded-xl border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">{statusLine}</div>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {[
          { label: "Deployments", value: overview?.summary.total_apps ?? 0, note: "Across projects" },
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
                  <TableHead>Deployment</TableHead>
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
                        href={`/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(failure.project_id)}/deployments`}
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
          <CardTitle className="text-base">All deployments</CardTitle>
          <CardDescription>Cross-project managed deployment status across the tenant.</CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {sortedApps.length === 0 ? (
            <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
              <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted">
                <Server className="h-6 w-6 text-muted-foreground" />
              </div>
              <p className="mt-4 text-sm font-medium">No deployments yet</p>
              <p className="mt-1 text-sm text-muted-foreground">
                Enable a project deployment policy to let MB create releases from GitHub commits.
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Project</TableHead>
                  <TableHead>Deployment</TableHead>
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
                        href={`/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(app.project_id)}/deployments`}
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
