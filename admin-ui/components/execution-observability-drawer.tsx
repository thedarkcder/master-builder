"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { Button } from "@/components/ui/button";
import { type WorkflowObservabilityEventRecord } from "@/lib/api";
import { formatTimeAgo, formatTimestamp } from "@/lib/datetime";
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
  activeView: "telemetry" | "audit";
  onViewChange: (view: "telemetry" | "audit") => void;
  onRefresh: () => void;
  onClose: () => void;
  telemetryEvents: WorkflowObservabilityEventRecord[];
  auditEvents: WorkflowObservabilityEventRecord[];
  loading: boolean;
  error: string | null;
}) {
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
    return () => setMounted(false);
  }, []);

  if (!open) {
    return null;
  }

  const events = activeView === "telemetry" ? telemetryEvents : auditEvents;

  if (!mounted) {
    return null;
  }

  return createPortal(
    (
    <div className="fixed inset-0 z-50">
      <button type="button" aria-label="Close drawer" className="absolute inset-0 bg-black/40" onClick={onClose} />
      <aside
        className="absolute top-0 right-0 bottom-0 flex w-full max-w-3xl flex-col overflow-hidden border-l bg-background shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="shrink-0 border-b bg-background px-6 py-5">
          <div className="flex items-start justify-between gap-3">
            <div className="space-y-2">
              <p className="text-sm text-muted-foreground">Execution step</p>
              <h2 className="text-xl font-semibold">{operationLabel}</h2>
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
        </div>

        <div className="shrink-0 overflow-x-auto border-b px-6">
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

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
          {error ? (
            <p className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</p>
          ) : null}

          {loading ? (
            <p className="mt-4 rounded-xl border px-4 py-3 text-sm text-muted-foreground">Loading events…</p>
          ) : null}

          <div className="mt-4 space-y-3">
            {!loading && events.length === 0 ? (
              <p className="rounded-xl border px-4 py-6 text-sm text-muted-foreground">No events recorded for this step yet.</p>
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
                  <span className="text-xs text-muted-foreground" title={formatTimestamp(event.recorded_at)}>
                    {formatTimeAgo(event.recorded_at)}
                  </span>
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
      </aside>
    </div>
    ),
    document.body,
  );
}
