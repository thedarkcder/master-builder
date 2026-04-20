"use client";

import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { type WorkflowObservabilityEventRecord } from "@/lib/api";
import { formatTimestamp } from "@/lib/datetime";
import { cn } from "@/lib/utils";

function levelTone(level: string): string {
  const normalized = String(level || "").trim().toLowerCase();
  if (normalized === "error") return "border-red-300 bg-red-50 text-red-700";
  if (normalized === "warn" || normalized === "warning") return "border-amber-300 bg-amber-50 text-amber-700";
  if (normalized === "debug") return "border-slate-300 bg-slate-50 text-slate-700";
  return "border-blue-300 bg-blue-50 text-blue-700";
}

export function ExecutionObservabilityDrawer({
  open,
  operationLabel,
  operationStatus,
  activeView,
  onViewChange,
  onRefresh,
  onClose,
  telemetryEvents,
  auditEvents,
  loading,
  error,
}: {
  open: boolean;
  operationLabel: string;
  operationStatus: string;
  activeView: "telemetry" | "audit";
  onViewChange: (view: "telemetry" | "audit") => void;
  onRefresh: () => void;
  onClose: () => void;
  telemetryEvents: WorkflowObservabilityEventRecord[];
  auditEvents: WorkflowObservabilityEventRecord[];
  loading: boolean;
  error: string | null;
}) {
  if (!open) {
    return null;
  }

  const events = activeView === "telemetry" ? telemetryEvents : auditEvents;

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/40" onClick={onClose}>
      <div
        className="h-full w-full max-w-3xl overflow-y-auto bg-background p-6 shadow-xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="space-y-2">
            <p className="text-sm text-muted-foreground">Execution step</p>
            <h2 className="text-xl font-semibold">{operationLabel}</h2>
            <div className="flex items-center gap-2">
              <StatusBadge status={operationStatus} />
              <span className="text-sm text-muted-foreground">
                {activeView === "telemetry" ? "Live telemetry" : "Audit history"}
              </span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={onRefresh} disabled={loading}>
              Refresh events
            </Button>
            <Button variant="outline" size="sm" onClick={onClose}>
              Close
            </Button>
          </div>
        </div>

        <div className="mt-4 overflow-x-auto border-b">
          <nav className="-mb-px flex min-w-max gap-0" aria-label="Execution observability tabs">
            {[
              { key: "telemetry", label: "Live telemetry" },
              { key: "audit", label: "Audit history" },
            ].map((tab) => {
              const selected = activeView === tab.key;
              return (
                <button
                  key={tab.key}
                  type="button"
                  onClick={() => onViewChange(tab.key as "telemetry" | "audit")}
                  className={cn(
                    "inline-flex items-center border-b-2 px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors",
                    selected
                      ? "border-primary text-foreground"
                      : "border-transparent text-muted-foreground hover:border-border hover:text-foreground",
                  )}
                >
                  {tab.label}
                </button>
              );
            })}
          </nav>
        </div>

        {error ? (
          <p className="mt-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</p>
        ) : null}

        {loading ? (
          <p className="mt-4 rounded-xl border px-4 py-3 text-sm text-muted-foreground">Loading {activeView === "telemetry" ? "telemetry" : "audit history"}…</p>
        ) : null}

        <div className="mt-4 space-y-3">
          {!loading && events.length === 0 ? (
            <p className="rounded-xl border px-4 py-6 text-sm text-muted-foreground">
              {activeView === "telemetry" ? "No live telemetry recorded for this step yet." : "No audit history recorded for this step yet."}
            </p>
          ) : null}

          {events.map((event) => (
            <div key={`${event.source}:${event.event_id}`} className="rounded-xl border p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={cn("rounded-full border px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide", levelTone(event.level))}>
                    {event.level}
                  </span>
                  <span className="rounded-full border px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                    {event.event_kind.replace(/_/g, " ")}
                  </span>
                  {event.source_component ? (
                    <span className="text-xs text-muted-foreground">{event.source_component}</span>
                  ) : null}
                </div>
                <span className="text-xs text-muted-foreground">{formatTimestamp(event.recorded_at)}</span>
              </div>
              <p className="mt-3 text-sm">{event.message}</p>
              {(event.invocation_id || event.stage || event.stream || event.agent_id) ? (
                <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted-foreground">
                  {event.agent_id ? <span>Agent: {event.agent_id}</span> : null}
                  {event.stage ? <span>Stage: {event.stage}</span> : null}
                  {event.stream ? <span>Stream: {event.stream}</span> : null}
                  {event.invocation_id ? <span>Invocation: {event.invocation_id}</span> : null}
                </div>
              ) : null}
              {event.payload && Object.keys(event.payload).length ? (
                <pre className="mt-3 overflow-x-auto rounded-lg bg-muted/50 p-3 text-xs text-muted-foreground">
                  {JSON.stringify(event.payload, null, 2)}
                </pre>
              ) : null}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
