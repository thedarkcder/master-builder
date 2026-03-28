"use client";

import { useEffect, useMemo, useState } from "react";
import { Activity, Bot, RefreshCw, Workflow } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { getPlatformStatus, type PlatformServiceInstanceRecord, type PlatformServiceStatusRecord } from "@/lib/api";
import { canAccessPlatformAdmin, getDefaultAuthenticatedRoute } from "@/lib/auth-routing";
import { cn } from "@/lib/utils";

const serviceIcons = {
  api: Activity,
  workers: Workflow,
  knowledge_jira_sync: RefreshCw,
  discord_commands: Bot,
} as const;

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

function InstanceCard({ instance }: { instance: PlatformServiceInstanceRecord }) {
  const heartbeatAt = instance.last_heartbeat_at ?? instance.updated_at;

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
        {instance.current_run_id ? <span>Run {instance.current_run_id}</span> : null}
      </div>
    </div>
  );
}

function ServiceRow({ service }: { service: PlatformServiceStatusRecord }) {
  const Icon = serviceIcons[service.service_id as keyof typeof serviceIcons] ?? Activity;
  const hasInstances = (service.instances?.length ?? 0) > 0;

  return (
    <div className="grid gap-4 border-t px-6 py-5 first:border-t-0">
      <div className="min-w-0 space-y-2">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border bg-muted/30">
            <Icon className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <h2 className="text-base font-semibold">{service.label}</h2>
            <p className="text-sm text-muted-foreground">{service.summary}</p>
          </div>
        </div>
        {service.capabilities.length ? (
          <div className="flex flex-wrap gap-2 pl-14">
            {service.capabilities.map((capability) => (
              <span
                key={capability}
                className="rounded-full border px-2.5 py-1 text-xs font-medium text-muted-foreground"
              >
                {capability}
              </span>
            ))}
          </div>
        ) : null}
      </div>
      <div className="flex flex-col items-start gap-2">
        <span className={cn("rounded-full border px-2.5 py-1 text-xs font-semibold capitalize", statusClasses(service.status))}>
          {service.status}
        </span>
        <p className="text-xs text-muted-foreground">{formatTimestamp(service.updated_at)}</p>
      </div>
      {hasInstances ? (
        <div className="space-y-3 rounded-xl border bg-muted/10 p-4">
          <p className="text-xs font-medium uppercase tracking-[0.2em] text-muted-foreground">
            {service.service_id === "workers" ? "Worker instances" : "Service instances"}
          </p>
          <div className="grid gap-3 md:grid-cols-2">
            {service.instances?.map((instance) => (
              <InstanceCard key={instance.instance_id} instance={instance} />
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default function PlatformStatusPage() {
  const { credentials, principal, principalReady, ready } = useAuth();
  const [services, setServices] = useState<PlatformServiceStatusRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const counts = useMemo(() => {
    return {
      healthy: services.filter((service) => service.status === "healthy").length,
      degraded: services.filter((service) => service.status === "degraded").length,
      idle: services.filter((service) => service.status === "idle").length,
      unavailable: services.filter((service) => service.status === "unavailable").length,
    };
  }, [services]);

  async function loadStatus() {
    if (!credentials) {
      return;
    }
    setLoading(true);
    setErrorMessage(null);
    try {
      const payload = await getPlatformStatus(credentials);
      setServices(payload.services);
    } catch (error) {
      setErrorMessage(`Failed to load platform status: ${(error as Error).message}`);
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
      void loadStatus();
    }
  }, [credentials, principal, principalReady, ready]);

  if (!principalReady) {
    return <main className="p-8 text-sm text-muted-foreground">Loading platform status...</main>;
  }

  if (principal && !canAccessPlatformAdmin(principal)) {
    return <main className="p-8 text-sm text-muted-foreground">Redirecting...</main>;
  }

  return (
    <div className="mx-auto w-full max-w-5xl space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-3xl font-semibold tracking-tight">Platform status</h1>
          <p className="text-sm text-muted-foreground">
            Health of the hosted Master Builder services and worker capability lanes.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void loadStatus()} disabled={loading}>
          <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", loading && "animate-spin")} />
          Refresh
        </Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-4">
        {[
          { label: "Healthy", value: counts.healthy },
          { label: "Degraded", value: counts.degraded },
          { label: "Idle", value: counts.idle },
          { label: "Unavailable", value: counts.unavailable },
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
        <div className="border-b px-6 py-4">
          <h2 className="text-base font-semibold">Hosted services</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            This page tracks product-facing services only. Internal infrastructure is excluded.
          </p>
        </div>

        {loading ? (
          <div className="space-y-4 px-6 py-6">
            {[...Array(4)].map((_, index) => (
              <Skeleton key={index} className="h-20 w-full rounded-xl" />
            ))}
          </div>
        ) : (
          <div>
            {services.map((service) => (
              <ServiceRow key={service.service_id} service={service} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
