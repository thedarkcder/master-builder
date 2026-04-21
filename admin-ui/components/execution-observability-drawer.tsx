"use client";

import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";

import { Button } from "@/components/ui/button";
import { type WorkflowStepAttemptTranscriptRecord, type WorkflowStepTranscriptRecord } from "@/lib/api";
import { formatTimeAgo, formatTimestamp } from "@/lib/datetime";
import { cn } from "@/lib/utils";

function levelTone(level: string): string {
  const normalized = String(level || "").trim().toLowerCase();
  if (normalized === "error") return "border-red-300 bg-red-50 text-red-700";
  if (normalized === "warn" || normalized === "warning") return "border-amber-300 bg-amber-50 text-amber-700";
  if (normalized === "debug") return "border-slate-300 bg-slate-50 text-slate-700";
  return "border-blue-300 bg-blue-50 text-blue-700";
}

function statusTone(status: string): string {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "failed") return "border-red-300 bg-red-50 text-red-700";
  if (normalized === "completed") return "border-emerald-300 bg-emerald-50 text-emerald-700";
  if (normalized === "running" || normalized === "retrying") return "border-blue-300 bg-blue-50 text-blue-700";
  if (normalized === "waiting_for_input") return "border-amber-300 bg-amber-50 text-amber-700";
  return "border-slate-300 bg-slate-50 text-slate-700";
}

function firstAction(transcript: WorkflowStepTranscriptRecord | null): string | null {
  return transcript?.attempts[0]?.recommended_next_action?.trim() || null;
}

export function ExecutionObservabilityDrawer({
  open,
  operationLabel,
  activeView,
  onViewChange,
  onRefresh,
  onClose,
  telemetryTranscript,
  auditTranscript,
  loading,
  error,
}: {
  open: boolean;
  operationLabel: string;
  activeView: "telemetry" | "audit";
  onViewChange: (view: "telemetry" | "audit") => void;
  onRefresh: () => void;
  onClose: () => void;
  telemetryTranscript: WorkflowStepTranscriptRecord | null;
  auditTranscript: WorkflowStepTranscriptRecord | null;
  loading: boolean;
  error: string | null;
}) {
  const [mounted, setMounted] = useState(false);
  const [selectedAttemptId, setSelectedAttemptId] = useState<string | null>(null);

  useEffect(() => {
    setMounted(true);
    return () => setMounted(false);
  }, []);

  const transcript = activeView === "telemetry" ? telemetryTranscript : auditTranscript;
  const attempts = transcript?.attempts ?? [];

  useEffect(() => {
    if (!open) {
      setSelectedAttemptId(null);
      return;
    }
    if (!attempts.length) {
      setSelectedAttemptId(null);
      return;
    }
    if (selectedAttemptId && attempts.some((attempt) => attempt.attempt_id === selectedAttemptId)) {
      return;
    }
    setSelectedAttemptId(attempts[0].attempt_id);
  }, [attempts, open, selectedAttemptId]);

  const selectedAttempt = useMemo(
    () => attempts.find((attempt) => attempt.attempt_id === selectedAttemptId) ?? attempts[0] ?? null,
    [attempts, selectedAttemptId],
  );

  if (!open || !mounted) {
    return null;
  }

  return createPortal(
    <div className="fixed inset-0 z-50">
      <button type="button" aria-label="Close drawer" className="absolute inset-0 bg-black/40" onClick={onClose} />
      <aside
        className="absolute top-0 right-0 bottom-0 flex w-full max-w-4xl flex-col overflow-hidden border-l bg-background shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="shrink-0 border-b bg-background px-6 py-5">
          <div className="flex items-start justify-between gap-3">
            <div className="space-y-2">
              <p className="text-sm text-muted-foreground">Execution step</p>
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-xl font-semibold">{operationLabel}</h2>
                {transcript?.current_status ? (
                  <span className={cn("rounded-full border px-2 py-0.5 text-xs font-medium", statusTone(transcript.current_status))}>
                    {transcript.current_status.replace(/_/g, " ")}
                  </span>
                ) : null}
              </div>
              {firstAction(transcript) ? <p className="text-sm text-muted-foreground">{firstAction(transcript)}</p> : null}
            </div>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" onClick={onRefresh} disabled={loading}>
                Refresh
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

        <div className="min-h-0 flex-1 overflow-hidden">
          <div className="grid h-full min-h-0 grid-cols-[240px_minmax(0,1fr)]">
            <div className="min-h-0 overflow-y-auto border-r bg-muted/20 px-4 py-4">
              <div className="space-y-2">
                {!loading && !attempts.length ? (
                  <p className="rounded-xl border bg-background px-3 py-4 text-sm text-muted-foreground">No transcript recorded for this step yet.</p>
                ) : null}
                {attempts.map((attempt) => {
                  const selected = selectedAttempt?.attempt_id === attempt.attempt_id;
                  return (
                    <button
                      key={attempt.attempt_id}
                      type="button"
                      onClick={() => setSelectedAttemptId(attempt.attempt_id)}
                      className={cn(
                        "w-full rounded-xl border bg-background px-3 py-3 text-left transition-colors",
                        selected ? "border-primary ring-1 ring-primary/20" : "hover:border-border",
                      )}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-sm font-semibold">Attempt {attempt.attempt_number}</span>
                        <span className={cn("rounded-full border px-2 py-0.5 text-[11px] font-medium", statusTone(attempt.status))}>
                          {attempt.status.replace(/_/g, " ")}
                        </span>
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground" title={attempt.finished_at ? formatTimestamp(attempt.finished_at) : undefined}>
                        {attempt.finished_at ? formatTimeAgo(attempt.finished_at) : attempt.started_at ? "In progress" : "Not started"}
                      </p>
                      {attempt.recommended_next_action ? (
                        <p className="mt-2 line-clamp-3 text-xs text-muted-foreground">{attempt.recommended_next_action}</p>
                      ) : null}
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="min-h-0 overflow-y-auto px-6 py-4">
              {error ? <p className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</p> : null}
              {loading ? <p className="rounded-xl border px-4 py-3 text-sm text-muted-foreground">Loading transcript…</p> : null}
              {!loading && selectedAttempt ? (
                <div className="space-y-5">
                  <section className="rounded-xl border bg-muted/20 p-4">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-base font-semibold">Attempt {selectedAttempt.attempt_number}</span>
                      <span className={cn("rounded-full border px-2 py-0.5 text-xs font-medium", statusTone(selectedAttempt.status))}>
                        {selectedAttempt.status.replace(/_/g, " ")}
                      </span>
                    </div>
                    <div className="mt-3 grid gap-3 text-sm md:grid-cols-2">
                      <div>
                        <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">Started</p>
                        <p className="mt-1">{selectedAttempt.started_at ? formatTimeAgo(selectedAttempt.started_at) : "—"}</p>
                      </div>
                      <div>
                        <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">Finished</p>
                        <p className="mt-1">{selectedAttempt.finished_at ? formatTimeAgo(selectedAttempt.finished_at) : "—"}</p>
                      </div>
                      <div>
                        <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">Duration</p>
                        <p className="mt-1">{selectedAttempt.duration_ms !== null ? `${selectedAttempt.duration_ms} ms` : "—"}</p>
                      </div>
                      <div>
                        <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">Next action</p>
                        <p className="mt-1">{selectedAttempt.recommended_next_action || "—"}</p>
                      </div>
                    </div>
                  </section>

                  {selectedAttempt.sections.map((section) => (
                    <section key={section.kind} className="space-y-3">
                      <div className="flex items-center justify-between gap-2">
                        <h3 className="text-sm font-semibold">{section.label}</h3>
                        <span className="text-xs text-muted-foreground">{section.entries.length} entr{section.entries.length === 1 ? "y" : "ies"}</span>
                      </div>
                      <div className="space-y-3">
                        {section.entries.map((entry) => (
                          <div key={entry.entry_id} className="rounded-xl border p-4">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <div className="flex flex-wrap items-center gap-2">
                                <span className={cn("rounded-full border px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide", levelTone(entry.level))}>
                                  {entry.level}
                                </span>
                                <span className="text-sm font-medium">{entry.title}</span>
                                {entry.source_component ? (
                                  <span className="text-xs text-muted-foreground">{entry.source_component}</span>
                                ) : null}
                              </div>
                              <span className="text-xs text-muted-foreground" title={formatTimestamp(entry.recorded_at)}>
                                {formatTimeAgo(entry.recorded_at)}
                              </span>
                            </div>
                            <p className="mt-3 text-sm whitespace-pre-wrap">{entry.message}</p>
                            {Object.keys(entry.payload ?? {}).length ? (
                              <pre className="mt-3 overflow-x-auto rounded-lg bg-muted/50 p-3 text-xs text-muted-foreground">
                                {JSON.stringify(entry.payload, null, 2)}
                              </pre>
                            ) : null}
                          </div>
                        ))}
                      </div>
                    </section>
                  ))}
                </div>
              ) : null}
            </div>
          </div>
        </div>
      </aside>
    </div>,
    document.body,
  );
}
