"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Activity, RefreshCw, Workflow } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { getPlatformStatus, type PlatformServiceInstanceRecord, type PlatformServiceStatusRecord } from "@/lib/api";
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
  const [loading, setLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const instances = workers?.instances ?? [];

  const counts = useMemo(() => {
    return {
      fresh: instances.filter((i) => i.status !== "stale" && i.status !== "stopped").length,
      busy: instances.filter((i) => i.status === "busy").length,
      stale: instances.filter((i) => i.status === "stale").length,
      stopped: instances.filter((i) => i.status === "stopped").length,
    };
  }, [instances]);

  async function loadWorkers() {
    if (!credentials) {
      return;
    }
    setLoading(true);
    setErrorMessage(null);
    try {
      const payload = await getPlatformStatus(credentials);
      const svc = payload.services.find((s) => s.service_id === "workers");
      setWorkers(svc ?? null);
    } catch (error) {
      setErrorMessage(`Failed to load worker runtimes: ${(error as Error).message}`);
    } finally {
      setLoading(false);
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
    }
  }, [credentials, principal, principalReady, ready]);

  if (!principalReady) {
    return <main className="p-8 text-sm text-muted-foreground">Loading agent runtimes...</main>;
  }

  if (principal && !canAccessPlatformAdmin(principal)) {
    return <main className="p-8 text-sm text-muted-foreground">Redirecting...</main>;
  }

  return (
    <div className="mx-auto w-full max-w-5xl space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-3xl font-semibold tracking-tight">Agent runtimes</h1>
          <p className="text-sm text-muted-foreground">
            Registered worker processes and heartbeat state (from{" "}
            <Link href="/status" className="text-primary underline-offset-4 hover:underline">
              platform status
            </Link>
            ).
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void loadWorkers()} disabled={loading}>
          <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", loading && "animate-spin")} />
          Refresh
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

      {errorMessage ? (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-5 py-4 text-sm text-destructive">
          {errorMessage}
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

        {loading ? (
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
    </div>
  );
}
