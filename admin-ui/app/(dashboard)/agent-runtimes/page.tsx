"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Activity, Bot, RefreshCw, RotateCcw, Save, Workflow } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import {
  getAgentRuntimeRouting,
  getPlatformStatus,
  resetAgentRuntimeRouting,
  updateAgentRuntimeRouting,
  type AgentExecutionProfileRecord,
  type AgentRuntimeRoutingRecord,
  type PlatformServiceInstanceRecord,
  type PlatformServiceStatusRecord,
} from "@/lib/api";
import { canAccessPlatformAdmin, getDefaultAuthenticatedRoute } from "@/lib/auth-routing";
import { cn } from "@/lib/utils";

function statusClasses(status: string): string {
  const normalized = status.toLowerCase();
  if (normalized === "healthy") {
    return "border-emerald-200 bg-emerald-50 text-emerald-800";
  }
  if (normalized === "degraded") {
    return "border-amber-200 bg-amber-50 text-amber-800";
  }
  if (normalized === "idle") {
    return "border-slate-200 bg-slate-50 text-slate-700";
  }
  if (normalized === "busy") {
    return "border-sky-200 bg-sky-50 text-sky-800";
  }
  if (normalized === "stale") {
    return "border-amber-200 bg-amber-50 text-amber-800";
  }
  if (normalized === "stopped") {
    return "border-rose-200 bg-rose-50 text-rose-800";
  }
  if (normalized === "starting") {
    return "border-violet-200 bg-violet-50 text-violet-800";
  }
  return "border-zinc-200 bg-zinc-50 text-zinc-700";
}

function formatTimestamp(value: string | null): string {
  if (!value) {
    return "No recent update";
  }
  return new Date(value).toLocaleString();
}

function formatInstanceStatus(status: string): string {
  const normalized = status.toLowerCase();
  if (normalized === "busy") {
    return "busy";
  }
  if (normalized === "stale") {
    return "stale";
  }
  if (normalized === "stopped") {
    return "stopped";
  }
  return normalized;
}

function profileSummary(profile: AgentExecutionProfileRecord | undefined): string {
  if (!profile) return "No profile selected";
  const parts = [
    profile.runtime_kind,
    profile.cli_command,
    profile.model,
    profile.reasoning_effort ?? "no reasoning override",
    profile.tool_bridge_allowed ? "tools on" : "tools off",
  ];
  if (profile.fallback_profile) {
    parts.push(`fallback ${profile.fallback_profile}`);
  }
  return parts.join(" · ");
}

function WorkerInstanceCard({ instance }: { instance: PlatformServiceInstanceRecord }) {
  const heartbeatAt = instance.last_heartbeat_at ?? instance.updated_at;
  const activeRuns = instance.active_run_count ?? 0;

  return (
    <div className="rounded-xl border bg-muted/20 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <p className="text-sm font-semibold">{instance.label}</p>
          <p className="font-mono text-xs text-muted-foreground">{instance.instance_id}</p>
          {instance.summary ? <p className="text-sm text-muted-foreground">{instance.summary}</p> : null}
        </div>
        <span
          className={cn(
            "rounded-full border px-2.5 py-1 text-xs font-semibold capitalize",
            statusClasses(instance.status),
          )}
        >
          {formatInstanceStatus(instance.status)}
        </span>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        {instance.capabilities.length ? (
          instance.capabilities.map((capability) => (
            <span
              key={capability}
              className="rounded-full border px-2.5 py-1 text-[11px] font-medium text-muted-foreground"
            >
              {capability}
            </span>
          ))
        ) : (
          <span className="text-xs text-muted-foreground">No capability lanes reported.</span>
        )}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
        <span>{formatTimestamp(heartbeatAt)}</span>
        {activeRuns > 0 ? (
          <span>
            {activeRuns} active run{activeRuns === 1 ? "" : "s"}
          </span>
        ) : null}
        {instance.current_run_id ? <span>Run {instance.current_run_id}</span> : null}
      </div>
    </div>
  );
}

export default function AgentRuntimesPage() {
  const { credentials, principal, principalReady, ready } = useAuth();
  const [workers, setWorkers] = useState<PlatformServiceStatusRecord | null>(null);
  const [workersLoading, setWorkersLoading] = useState(true);
  const [workerErrorMessage, setWorkerErrorMessage] = useState<string | null>(null);

  const [routing, setRouting] = useState<AgentRuntimeRoutingRecord | null>(null);
  const [roleRouting, setRoleRouting] = useState<Record<string, string>>({});
  const [nameRouting, setNameRouting] = useState<Record<string, string>>({});
  const [routingLoading, setRoutingLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [routingStatusLine, setRoutingStatusLine] = useState("");

  const instances = workers?.instances ?? [];

  const counts = useMemo(() => {
    return {
      fresh: instances.filter((i) => i.status !== "stale" && i.status !== "stopped").length,
      busy: instances.filter((i) => i.status === "busy").length,
      stale: instances.filter((i) => i.status === "stale").length,
      stopped: instances.filter((i) => i.status === "stopped").length,
    };
  }, [instances]);

  const loadWorkers = useCallback(async (): Promise<void> => {
    if (!credentials) {
      return;
    }
    setWorkersLoading(true);
    setWorkerErrorMessage(null);
    try {
      const payload = await getPlatformStatus(credentials);
      const svc = payload.services.find((s) => s.service_id === "workers");
      setWorkers(svc ?? null);
    } catch (error) {
      setWorkerErrorMessage(`Failed to load worker runtimes: ${(error as Error).message}`);
    } finally {
      setWorkersLoading(false);
    }
  }, [credentials]);

  const refreshRouting = useCallback(async (): Promise<void> => {
    if (!credentials) return;
    setRoutingLoading(true);
    try {
      const response = await getAgentRuntimeRouting(credentials);
      setRouting(response);
      setRoleRouting(response.role_routing);
      setNameRouting(response.name_routing);
      setRoutingStatusLine("Loaded platform agent runtime routing.");
    } catch (error) {
      setRoutingStatusLine(`Failed to load agent runtimes: ${(error as Error).message}`);
    } finally {
      setRoutingLoading(false);
    }
  }, [credentials]);

  const profiles = routing?.available_profiles ?? {};
  const sortedProfileNames = useMemo(() => Object.keys(profiles).sort(), [profiles]);

  async function save(): Promise<void> {
    if (!credentials) return;
    setSaving(true);
    try {
      const response = await updateAgentRuntimeRouting(credentials, {
        role_routing: Object.fromEntries(Object.entries(roleRouting).filter(([, value]) => value)),
        name_routing: Object.fromEntries(Object.entries(nameRouting).filter(([, value]) => value)),
      });
      setRouting(response);
      setRoleRouting(response.role_routing);
      setNameRouting(response.name_routing);
      setRoutingStatusLine("Saved platform agent runtime routing.");
    } catch (error) {
      setRoutingStatusLine(`Save failed: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function reset(): Promise<void> {
    if (!credentials) return;
    if (!window.confirm("Reset all platform agent runtime overrides?")) return;
    setSaving(true);
    try {
      const response = await resetAgentRuntimeRouting(credentials);
      setRouting(response);
      setRoleRouting(response.role_routing);
      setNameRouting(response.name_routing);
      setRoutingStatusLine("Reset platform agent runtime routing to inherited defaults.");
    } catch (error) {
      setRoutingStatusLine(`Reset failed: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  useEffect(() => {
    if (!ready || !principalReady || !principal) {
      return;
    }
    if (!canAccessPlatformAdmin(principal)) {
      window.location.replace(getDefaultAuthenticatedRoute(principal));
      return;
    }
    if (credentials) {
      void loadWorkers();
      void refreshRouting();
    }
  }, [credentials, loadWorkers, principal, principalReady, ready, refreshRouting]);

  if (!principalReady) {
    return <main className="p-8 text-sm text-muted-foreground">Loading agent runtimes...</main>;
  }

  if (principal && !canAccessPlatformAdmin(principal)) {
    return <main className="p-8 text-sm text-muted-foreground">Redirecting...</main>;
  }

  return (
    <div className="mx-auto w-full max-w-5xl space-y-10">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-3xl font-semibold tracking-tight">Agent runtimes</h1>
          <p className="text-sm text-muted-foreground">
            Worker heartbeats and active runs (from{" "}
            <Link href="/status" className="text-primary underline-offset-4 hover:underline">
              platform status
            </Link>
            ), plus platform-wide routing from agent roles and named agents to runtime profiles.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void loadWorkers()} disabled={workersLoading}>
          <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", workersLoading && "animate-spin")} />
          Refresh workers
        </Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-4">
        {[
          { label: "Online", value: counts.fresh },
          { label: "Busy", value: counts.busy },
          { label: "Stale", value: counts.stale },
          { label: "Stopped", value: counts.stopped },
        ].map((item) => (
          <div key={item.label} className="border-b pb-3">
            <p className="text-xs font-medium uppercase tracking-[0.2em] text-muted-foreground">{item.label}</p>
            <p className="mt-2 text-2xl font-semibold">{item.value}</p>
          </div>
        ))}
      </div>

      {workerErrorMessage ? (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-5 py-4 text-sm text-destructive">
          {workerErrorMessage}
        </div>
      ) : null}

      <section className="overflow-hidden rounded-2xl border bg-background">
        <div className="flex flex-wrap items-start justify-between gap-4 border-b px-6 py-4">
          <div className="flex min-w-0 items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border bg-muted/30">
              <Workflow className="h-4 w-4" />
            </div>
            <div className="min-w-0">
              <h2 className="text-base font-semibold">Workers</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                {workers?.summary ?? "Heartbeat and active-run counts from worker runtime registrations."}
              </p>
            </div>
          </div>
          {workers ? (
            <span
              className={cn(
                "rounded-full border px-2.5 py-1 text-xs font-semibold capitalize",
                statusClasses(workers.status),
              )}
            >
              {workers.status}
            </span>
          ) : null}
        </div>

        {workersLoading ? (
          <div className="space-y-4 px-6 py-6">
            {[...Array(3)].map((_, index) => (
              <Skeleton key={index} className="h-28 w-full rounded-xl" />
            ))}
          </div>
        ) : instances.length === 0 ? (
          <div className="flex flex-col items-center gap-2 px-6 py-12 text-center">
            <Activity className="h-8 w-8 text-muted-foreground/60" />
            <p className="text-sm text-muted-foreground">No worker runtime registrations yet.</p>
          </div>
        ) : (
          <div className="p-6">
            <div className="grid gap-3 md:grid-cols-2">
              {instances.map((instance) => (
                <WorkerInstanceCard key={instance.instance_id} instance={instance} />
              ))}
            </div>
          </div>
        )}
      </section>

      <div className="space-y-6">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10">
              <Bot className="h-5 w-5 text-primary" />
            </div>
            <div>
              <h2 className="text-xl font-semibold">Runtime routing</h2>
              <p className="text-sm text-muted-foreground">
                Manage platform-wide routing from agent roles and named agents to built-in runtime profiles.
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => void refreshRouting()} disabled={routingLoading || saving}>
              <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${routingLoading ? "animate-spin" : ""}`} />
              Refresh routing
            </Button>
            <Button variant="outline" size="sm" onClick={() => void reset()} disabled={routingLoading || saving}>
              <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
              Reset
            </Button>
            <Button size="sm" onClick={() => void save()} disabled={routingLoading || saving || !routing}>
              <Save className="mr-1.5 h-3.5 w-3.5" />
              Save
            </Button>
          </div>
        </div>
        {routingStatusLine ? (
          <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{routingStatusLine}</p>
        ) : null}

        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between gap-2">
              <CardTitle className="text-base">Routing precedence</CardTitle>
              {routing ? (
                <Badge variant="outline" className="text-xs">
                  named agent overrides role, role overrides selector/default
                </Badge>
              ) : null}
            </div>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            Use a named-agent override only when one specific agent needs a different runtime. Leave a row blank to
            inherit the lower-precedence default.
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Role defaults</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Role</TableHead>
                  <TableHead>Override</TableHead>
                  <TableHead>Default</TableHead>
                  <TableHead>Effective profile</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(routing?.available_roles ?? []).map((role) => {
                  const selectedProfile = roleRouting[role] ?? "";
                  const fallbackProfile = routing?.effective_defaults.role_routing[role] ?? "";
                  const effectiveProfileName = selectedProfile || fallbackProfile;
                  return (
                    <TableRow key={role}>
                      <TableCell className="font-medium">{role}</TableCell>
                      <TableCell className="min-w-[240px]">
                        <select
                          className="h-9 w-full rounded border border-input bg-background px-3 text-sm"
                          value={selectedProfile}
                          onChange={(e) => setRoleRouting((current) => ({ ...current, [role]: e.target.value }))}
                          disabled={routingLoading || saving}
                        >
                          <option value="">Inherit default</option>
                          {sortedProfileNames.map((profileName) => (
                            <option key={profileName} value={profileName}>
                              {profileName}
                            </option>
                          ))}
                        </select>
                      </TableCell>
                      <TableCell className="font-mono text-xs">{fallbackProfile || "—"}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        <p className="font-mono text-foreground">{effectiveProfileName || "—"}</p>
                        <p>{profileSummary(profiles[effectiveProfileName])}</p>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Named-agent overrides</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Named agent</TableHead>
                  <TableHead>Override</TableHead>
                  <TableHead>Default</TableHead>
                  <TableHead>Effective profile</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(routing?.available_named_agents ?? []).map((agentName) => {
                  const selectedProfile = nameRouting[agentName] ?? "";
                  const fallbackProfile = routing?.effective_defaults.name_routing[agentName] ?? "";
                  const effectiveProfileName = selectedProfile || fallbackProfile;
                  return (
                    <TableRow key={agentName}>
                      <TableCell className="font-medium">{agentName}</TableCell>
                      <TableCell className="min-w-[240px]">
                        <select
                          className="h-9 w-full rounded border border-input bg-background px-3 text-sm"
                          value={selectedProfile}
                          onChange={(e) => setNameRouting((current) => ({ ...current, [agentName]: e.target.value }))}
                          disabled={routingLoading || saving}
                        >
                          <option value="">Inherit default</option>
                          {sortedProfileNames.map((profileName) => (
                            <option key={profileName} value={profileName}>
                              {profileName}
                            </option>
                          ))}
                        </select>
                      </TableCell>
                      <TableCell className="font-mono text-xs">{fallbackProfile || "—"}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        <p className="font-mono text-foreground">{effectiveProfileName || "—"}</p>
                        <p>{profileSummary(profiles[effectiveProfileName])}</p>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">Available profiles</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Profile</TableHead>
                  <TableHead>Runtime</TableHead>
                  <TableHead>CLI</TableHead>
                  <TableHead>Model</TableHead>
                  <TableHead>Reasoning</TableHead>
                  <TableHead>Fallback</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sortedProfileNames.map((profileName) => {
                  const profile = profiles[profileName];
                  return (
                    <TableRow key={profileName}>
                      <TableCell className="font-mono text-xs">{profileName}</TableCell>
                      <TableCell>{profile.runtime_kind}</TableCell>
                      <TableCell className="font-mono text-xs">{profile.cli_command}</TableCell>
                      <TableCell className="font-mono text-xs">{profile.model}</TableCell>
                      <TableCell>{profile.reasoning_effort ?? "—"}</TableCell>
                      <TableCell className="font-mono text-xs">{profile.fallback_profile ?? "—"}</TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
