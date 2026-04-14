"use client";

import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Activity, Bot, RefreshCw, Workflow } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  getWorkerRuntimeAuthRequest,
  getPlatformStatus,
  type PlatformRuntimeDependencyRecord,
  type PlatformStatusRecord,
  type PlatformServiceInstanceRecord,
  type PlatformServiceStatusRecord,
  type WorkerRuntimeAuthRequestRecord,
  startWorkerRuntimeLoginSession,
} from "@/lib/api";
import { canAccessPlatformAdmin, getDefaultAuthenticatedRoute } from "@/lib/auth-routing";
import { renderAnsiText } from "@/lib/ansi-render";
import { formatTimestamp } from "@/lib/datetime";
import { readLastWorkspaceTenantIdFromBrowser } from "@/lib/workspace-preference";
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

function formatRuntimeKindLabel(runtimeKind: string): string {
  const normalized = runtimeKind.trim().toLowerCase();
  if (normalized === "codex_cli") {
    return "Codex CLI";
  }
  if (normalized === "chat_cli") {
    return "Chat CLI";
  }
  if (normalized === "claude_cli") {
    return "Claude CLI";
  }
  return runtimeKind
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function runtimeStatusClasses(status: string | undefined): string {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "ready") {
    return "border-emerald-200 bg-emerald-50 text-emerald-800";
  }
  if (normalized === "degraded") {
    return "border-amber-200 bg-amber-50 text-amber-800";
  }
  if (normalized === "unavailable") {
    return "border-rose-200 bg-rose-50 text-rose-800";
  }
  return "border-zinc-200 bg-zinc-50 text-zinc-700";
}

function formatRuntimeLoginStatus(runtimeKind: string, status: string | undefined): string {
  const normalizedKind = runtimeKind.trim().toLowerCase();
  const normalizedStatus = String(status || "").trim().toLowerCase();
  const isCliRuntime = normalizedKind.endsWith("_cli");
  if (normalizedStatus === "ready") {
    return isCliRuntime ? "Logged in" : "Ready";
  }
  if (normalizedStatus === "degraded" || normalizedStatus === "unavailable") {
    return isCliRuntime ? "Not logged in" : "Not ready";
  }
  return normalizedStatus || "Unknown";
}

type RuntimeDependencyEntry = {
  runtimeKind: string;
  dependency: PlatformRuntimeDependencyRecord;
};

type RuntimeLoginModalState = {
  serviceInstanceId: string;
  runtimeKind: string;
  runtimeLabel: string;
  requestId: string | null;
  requestStatus: string | null;
  instructions: string | null;
  expiresAt: string | null;
  loading: boolean;
  error: string | null;
};

function RuntimeLoginModal({
  state,
  onClose,
}: {
  state: RuntimeLoginModalState | null;
  onClose: () => void;
}) {
  if (!state) {
    return null;
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 px-4 py-6">
      <div className="w-full max-w-3xl rounded-2xl border bg-background shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b px-6 py-4">
          <div className="space-y-1">
            <h2 className="text-lg font-semibold">Login {state.runtimeLabel}</h2>
            <p className="text-sm text-muted-foreground">
              {state.requestStatus === "completed"
                ? "Login completed. The worker reported this runtime as authenticated."
                : state.loading
                  ? "Starting a worker-owned login session and waiting for instructions."
                  : state.instructions
                    ? "Complete the login in your browser using the instructions below."
                    : "Login instructions are not available yet."}
            </p>
          </div>
          <Button type="button" variant="outline" size="sm" onClick={onClose}>
            Close
          </Button>
        </div>
        <div className="space-y-4 px-6 py-5">
          {state.requestStatus === "completed" ? (
            <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
              Login completed. This worker runtime is now authenticated.
            </div>
          ) : null}
          {state.error ? (
            <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
              {state.error}
            </div>
          ) : null}
          {state.expiresAt ? (
            <p className="text-xs text-muted-foreground">
              Instructions expire {formatTimestamp(state.expiresAt)}
            </p>
          ) : null}
          {state.instructions ? (
            <pre className="overflow-x-auto rounded-md border bg-muted/30 p-4 text-xs whitespace-pre-wrap text-foreground">
              {renderAnsiText(state.instructions)}
            </pre>
          ) : (
            <div className="rounded-md border bg-muted/20 p-4 text-sm text-muted-foreground">
              {state.loading ? "Waiting for worker instructions…" : "No instructions available."}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function InstanceCard({
  instance,
  startingRuntimeKind,
  onOpenRuntimeLogin,
}: {
  instance: PlatformServiceInstanceRecord;
  startingRuntimeKind: string | null;
  onOpenRuntimeLogin: (instance: PlatformServiceInstanceRecord, runtimeKind: string) => void;
}) {
  const heartbeatAt = instance.last_heartbeat_at ?? instance.updated_at;
  const runtimeDependencies = useMemo<RuntimeDependencyEntry[]>(() => {
    return Object.entries(instance.runtime_dependencies ?? {})
      .filter(([, dependency]) => dependency && (dependency.summary || dependency.remediation_text))
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([runtimeKind, dependency]) => ({ runtimeKind, dependency }));
  }, [instance.runtime_dependencies]);

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
        <span>{formatTimestamp(heartbeatAt, "No recent update")}</span>
        {instance.current_run_id ? <span>Run {instance.current_run_id}</span> : null}
      </div>
      {runtimeDependencies.length ? (
        <div className="mt-4 space-y-3 border-t pt-4">
          <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
            Runtime dependencies
          </p>
          <div className="space-y-3">
            {runtimeDependencies.map(({ runtimeKind, dependency }) => {
              const remediationText = dependency.remediation_text?.trim() || null;
              const isStarting = startingRuntimeKind === `${instance.instance_id}:${runtimeKind}`;
              return (
                <div key={runtimeKind} className="rounded-lg border bg-background/80 p-3">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="text-sm font-medium">{formatRuntimeKindLabel(runtimeKind)}</p>
                        <span
                          className={cn(
                            "rounded-full border px-2 py-0.5 text-[11px] font-semibold capitalize",
                            runtimeStatusClasses(dependency.state),
                          )}
                        >
                          {dependency.state ?? "unknown"}
                        </span>
                      </div>
                      {dependency.summary ? (
                        <p className="text-sm text-muted-foreground">{dependency.summary}</p>
                      ) : null}
                      <p className="text-xs text-muted-foreground">
                        Status: {formatRuntimeLoginStatus(runtimeKind, dependency.state)}
                      </p>
                      {dependency.remediation_expires_at ? (
                        <p className="text-xs text-muted-foreground">
                          Instructions expire {formatTimestamp(dependency.remediation_expires_at)}
                        </p>
                      ) : null}
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      {dependency.state !== "ready" ? (
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          disabled={isStarting}
                          onClick={() => onOpenRuntimeLogin(instance, runtimeKind)}
                        >
                          {isStarting
                            ? "Starting login…"
                            : remediationText
                              ? "View login instructions"
                              : "Login"}
                        </Button>
                      ) : null}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}
      </div>
  );
}

function ServiceRow({
  service,
  startingRuntimeKind,
  onOpenRuntimeLogin,
}: {
  service: PlatformServiceStatusRecord;
  startingRuntimeKind: string | null;
  onOpenRuntimeLogin: (instance: PlatformServiceInstanceRecord, runtimeKind: string) => void;
}) {
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
        <div className="space-y-3 rounded-xl bg-muted/10 p-4">
          <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
            {service.service_id === "workers" ? "Worker instances" : "Service instances"}
          </p>
          <div className="grid gap-3 md:grid-cols-2">
            {service.instances?.map((instance) => (
              <InstanceCard
                key={instance.instance_id}
                instance={instance}
                startingRuntimeKind={startingRuntimeKind}
                onOpenRuntimeLogin={onOpenRuntimeLogin}
              />
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
  const [startingRuntimeKey, setStartingRuntimeKey] = useState<string | null>(null);
  const [loginModalState, setLoginModalState] = useState<RuntimeLoginModalState | null>(null);

  const counts = useMemo(() => {
    return {
      healthy: services.filter((service) => service.status === "healthy").length,
      degraded: services.filter((service) => service.status === "degraded").length,
      idle: services.filter((service) => service.status === "idle").length,
      unavailable: services.filter((service) => service.status === "unavailable").length,
    };
  }, [services]);

  async function fetchStatusSnapshot(): Promise<PlatformStatusRecord> {
    if (!credentials) {
      throw new Error("Missing credentials");
    }
    return getPlatformStatus(credentials);
  }

  async function loadStatus(): Promise<PlatformStatusRecord> {
    if (!credentials) {
      throw new Error("Missing credentials");
    }
    setLoading(true);
    setErrorMessage(null);
    try {
      const statusPayload = await fetchStatusSnapshot();
      setServices(statusPayload.services);
      return statusPayload;
    } catch (error) {
      setErrorMessage(`Failed to load platform status: ${(error as Error).message}`);
      throw error;
    } finally {
      setLoading(false);
    }
  }

  async function handleStartLoginSession(
    serviceInstanceId: string,
    runtimeKind: string,
  ): Promise<WorkerRuntimeAuthRequestRecord> {
    if (!credentials) {
      throw new Error("Missing credentials");
    }
    return startWorkerRuntimeLoginSession(credentials, serviceInstanceId, runtimeKind);
  }

  async function refreshStatusSilently(): Promise<void> {
    if (!credentials) {
      return;
    }
    try {
      const statusPayload = await fetchStatusSnapshot();
      setServices(statusPayload.services);
    } catch {
      // Keep the current page state if a background refresh fails.
    }
  }

  async function openRuntimeLoginModal(instance: PlatformServiceInstanceRecord, runtimeKind: string): Promise<void> {
    const runtimeLabel = formatRuntimeKindLabel(runtimeKind);
    const runtimeKey = `${instance.instance_id}:${runtimeKind}`;
    const existingDependency = instance.runtime_dependencies?.[runtimeKind] ?? null;
    const existingInstructions = existingDependency?.remediation_text?.trim() || null;

    setLoginModalState({
      serviceInstanceId: instance.instance_id,
      runtimeKind,
      runtimeLabel,
      requestId: null,
      requestStatus: null,
      instructions: existingInstructions,
      expiresAt: existingDependency?.remediation_expires_at ?? null,
      loading: !existingInstructions,
      error: null,
    });

    if (existingInstructions) {
      return;
    }

    setStartingRuntimeKey(runtimeKey);
    try {
      const request = await handleStartLoginSession(instance.instance_id, runtimeKind);
      setLoginModalState({
        serviceInstanceId: instance.instance_id,
        runtimeKind,
        runtimeLabel,
        requestId: request.request_id,
        requestStatus: request.status,
        instructions: request.remediation_text?.trim() || null,
        expiresAt: request.expires_at ?? null,
        loading: true,
        error: null,
      });
    } catch (error) {
      setLoginModalState({
        serviceInstanceId: instance.instance_id,
        runtimeKind,
        runtimeLabel,
        requestId: null,
        requestStatus: null,
        instructions: null,
        expiresAt: null,
        loading: false,
        error: `Failed to start login session: ${(error as Error).message}`,
      });
    } finally {
      setStartingRuntimeKey((current) => (current === runtimeKey ? null : current));
    }
  }

  useEffect(() => {
    if (!credentials || !loginModalState?.requestId) {
      return;
    }
    const requestId = loginModalState.requestId;
    let cancelled = false;

    const poll = async (): Promise<void> => {
      try {
        const request = await getWorkerRuntimeAuthRequest(credentials, requestId);
        if (cancelled) {
          return;
        }
        const remediationText = request.remediation_text?.trim() || null;
        const terminal = request.status === "completed" || request.status === "failed" || request.status === "expired";
        setLoginModalState((current) => {
          if (!current || current.requestId !== requestId) {
            return current;
          }
          return {
            ...current,
            requestStatus: request.status,
            instructions: remediationText ?? current.instructions,
            expiresAt: request.expires_at ?? current.expiresAt,
            loading: !terminal,
            error:
              request.status === "failed" || request.status === "expired"
                ? request.last_error?.trim() || "The worker login session ended before authentication completed."
                : null,
          };
        });
        if (terminal) {
          void refreshStatusSilently();
          return;
        }
        window.setTimeout(() => {
          void poll();
        }, 1000);
      } catch (error) {
        if (cancelled) {
          return;
        }
        setLoginModalState((current) => {
          if (!current || current.requestId !== requestId) {
            return current;
          }
          return {
            ...current,
            loading: false,
            error: `Failed to refresh login session: ${(error as Error).message}`,
          };
        });
      }
    };

    void poll();

    return () => {
      cancelled = true;
    };
  }, [credentials, loginModalState?.requestId]);

  useEffect(() => {
    if (!ready || !principalReady || !principal) {
      return;
    }
    if (!canAccessPlatformAdmin(principal)) {
      const preferredTenantId = readLastWorkspaceTenantIdFromBrowser();
      window.location.replace(getDefaultAuthenticatedRoute(principal, { preferredTenantId }));
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
    <div className="w-full space-y-6">
      <div className="flex flex-wrap items-center justify-end gap-3">
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
            <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">{item.label}</p>
            <p className="mt-2 text-2xl font-semibold">{item.value}</p>
          </div>
        ))}
      </div>

      {errorMessage ? (
        <div className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {errorMessage}
        </div>
      ) : null}

      <section className="overflow-hidden rounded-2xl border bg-background">
        <div className="border-b px-6 py-4">
          <h2 className="text-base font-semibold">Hosted services</h2>
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
              <ServiceRow
                key={service.service_id}
                service={service}
                startingRuntimeKind={startingRuntimeKey}
                onOpenRuntimeLogin={(instance, runtimeKind) => {
                  void openRuntimeLoginModal(instance, runtimeKind);
                }}
              />
            ))}
          </div>
        )}
      </section>
      <RuntimeLoginModal state={loginModalState} onClose={() => setLoginModalState(null)} />
    </div>
  );
}
