"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { Button } from "@/components/ui/button";
import {
  type WorkflowOperationAttemptRecord,
  type WorkflowStepAttemptTranscriptRecord,
} from "@/lib/api";
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
  if (normalized === "superseded") return "border-slate-300 bg-slate-50 text-slate-600";
  return "border-slate-300 bg-slate-50 text-slate-700";
}

function statusLabel(status: string): string {
  return String(status || "unknown").replace(/_/g, " ");
}

function latestAttemptNumber(attempts: WorkflowOperationAttemptRecord[]): number {
  return Math.max(0, ...attempts.map((attempt) => attempt.attempt_number));
}

function displayStatusForAttempt(
  attempt: WorkflowOperationAttemptRecord,
  options: { latestNumber: number },
): string {
  const normalized = String(attempt.status || "").trim().toLowerCase();
  if (normalized === "waiting_for_input" && attempt.attempt_number < options.latestNumber) {
    return "superseded";
  }
  return normalized || "unknown";
}

function attemptDuration(attempt: WorkflowOperationAttemptRecord): number | null {
  if (!attempt.started_at || !attempt.finished_at) {
    return null;
  }
  return Math.max(0, new Date(attempt.finished_at).getTime() - new Date(attempt.started_at).getTime());
}

function formatDuration(durationMs: number | null | undefined): string {
  if (typeof durationMs !== "number" || !Number.isFinite(durationMs)) {
    return "—";
  }
  const normalized = Math.max(0, Math.floor(durationMs));
  const totalSeconds = Math.floor(normalized / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}h ${minutes}m ${seconds}s`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds}s`;
  }
  if (seconds > 0 || normalized >= 1000) {
    return `${seconds}s`;
  }
  return `${normalized}ms`;
}

type AttemptMetric = {
  label: string;
  value: string;
};

export type ExecutionObservabilityView = "telemetry" | "metrics" | "audit";

function numberFromPayload(payload: Record<string, unknown>, key: string): number | null {
  const value = payload[key];
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function stringFromPayload(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key];
  if (typeof value !== "string") {
    return null;
  }
  const normalized = value.trim();
  return normalized || null;
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("en").format(Math.max(0, Math.floor(value)));
}

function latestRuntimePayload(
  attempt: WorkflowStepAttemptTranscriptRecord | null,
  eventKind: string,
): Record<string, unknown> | null {
  const entries =
    attempt?.sections.flatMap((section) =>
      section.entries.filter((entry) => {
        const payloadKind = stringFromPayload(entry.payload, "event_kind");
        return payloadKind === eventKind || entry.title.replace(/ /g, "_") === eventKind;
      }),
    ) ?? [];
  const directEntries =
    attempt?.sections.flatMap((section) =>
      section.entries.filter((entry) => {
        if (eventKind === "stage_invocation_finished") {
          return entry.title.endsWith("stage invocation finished") || entry.title === "stage invocation finished";
        }
        if (eventKind === "stage_invocation_started") {
          return entry.title.endsWith("stage invocation started") || entry.title === "stage invocation started";
        }
        return false;
      }),
    ) ?? [];
  const selected = [...entries, ...directEntries].at(-1);
  return selected?.payload ?? null;
}

function attemptMetrics(attempt: WorkflowStepAttemptTranscriptRecord | null): AttemptMetric[] {
  if (!attempt) {
    return [];
  }
  const finishedPayload = latestRuntimePayload(attempt, "stage_invocation_finished");
  const startedPayload = latestRuntimePayload(attempt, "stage_invocation_started");
  const metrics: AttemptMetric[] = [];

  const runtimeMs = finishedPayload ? numberFromPayload(finishedPayload, "duration_ms") : null;
  if (runtimeMs !== null) {
    metrics.push({ label: "Runtime", value: formatDuration(runtimeMs) });
  }
  const totalTokens = finishedPayload ? numberFromPayload(finishedPayload, "actual_total_tokens") : null;
  if (totalTokens !== null) {
    metrics.push({ label: "Tokens", value: formatNumber(totalTokens) });
  }
  const promptTokens = finishedPayload ? numberFromPayload(finishedPayload, "actual_prompt_tokens") : null;
  const completionTokens = finishedPayload ? numberFromPayload(finishedPayload, "actual_completion_tokens") : null;
  if (promptTokens !== null || completionTokens !== null) {
    metrics.push({
      label: "Token split",
      value: `${promptTokens === null ? "—" : formatNumber(promptTokens)} in / ${
        completionTokens === null ? "—" : formatNumber(completionTokens)
      } out`,
    });
  }
  const estimatedPromptTokens =
    (startedPayload ? numberFromPayload(startedPayload, "estimated_prompt_tokens") : null) ??
    (finishedPayload ? numberFromPayload(finishedPayload, "estimated_prompt_tokens") : null);
  if (estimatedPromptTokens !== null) {
    metrics.push({ label: "Estimated prompt", value: formatNumber(estimatedPromptTokens) });
  }
  const model = (finishedPayload && stringFromPayload(finishedPayload, "model")) || (startedPayload && stringFromPayload(startedPayload, "model"));
  if (model) {
    metrics.push({ label: "Model", value: model });
  }
  const reasoning =
    (finishedPayload && stringFromPayload(finishedPayload, "reasoning_effort")) ||
    (startedPayload && stringFromPayload(startedPayload, "reasoning_effort"));
  if (reasoning) {
    metrics.push({ label: "Reasoning", value: reasoning });
  }
  const persistedLines = finishedPayload ? numberFromPayload(finishedPayload, "db_persisted_lines") : null;
  const rawLines = finishedPayload ? numberFromPayload(finishedPayload, "raw_lines_written") : null;
  if (persistedLines !== null || rawLines !== null) {
    metrics.push({
      label: "Log lines",
      value: `${persistedLines === null ? "—" : formatNumber(persistedLines)} stored / ${
        rawLines === null ? "—" : formatNumber(rawLines)
      } raw`,
    });
  }
  const kbHits =
    (finishedPayload ? numberFromPayload(finishedPayload, "kb_hits") : null) ??
    (startedPayload ? numberFromPayload(startedPayload, "kb_hits") : null);
  if (kbHits !== null) {
    metrics.push({ label: "Knowledge hits", value: formatNumber(kbHits) });
  }
  return metrics;
}

export function ExecutionObservabilityDrawer({
  open,
  operationLabel,
  attempts,
  selectedAttemptId,
  onSelectAttemptId,
  activeView,
  onViewChange,
  onRefresh,
  onClose,
  currentStatus,
  telemetryAttempt,
  auditAttempt,
  waitingForNewAttempt,
  telemetryLoading,
  auditLoading,
  error,
}: {
  open: boolean;
  operationLabel: string;
  attempts: WorkflowOperationAttemptRecord[];
  selectedAttemptId: string | null;
  onSelectAttemptId: (attemptId: string | null) => void;
  activeView: ExecutionObservabilityView;
  onViewChange: (view: ExecutionObservabilityView) => void;
  onRefresh: () => void;
  onClose: () => void;
  currentStatus: string;
  telemetryAttempt: WorkflowStepAttemptTranscriptRecord | null;
  auditAttempt: WorkflowStepAttemptTranscriptRecord | null;
  waitingForNewAttempt: boolean;
  telemetryLoading: boolean;
  auditLoading: boolean;
  error: string | null;
}) {
  const [mounted, setMounted] = useState(false);
  const [autoScrollTelemetry, setAutoScrollTelemetry] = useState(true);
  const contentScrollRef = useRef<HTMLDivElement | null>(null);
  const telemetryBottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setMounted(true);
    return () => setMounted(false);
  }, []);

  const selectedPersistedAttempt = useMemo(
    () => attempts.find((attempt) => attempt.attempt_id === selectedAttemptId) ?? null,
    [attempts, selectedAttemptId],
  );
  const latestNumber = useMemo(() => latestAttemptNumber(attempts), [attempts]);
  const selectedDisplayStatus = selectedPersistedAttempt
    ? displayStatusForAttempt(selectedPersistedAttempt, { latestNumber })
    : "unknown";
  const renderedAttempt = activeView === "audit" ? auditAttempt : telemetryAttempt;
  const loading = activeView === "audit" ? auditLoading : telemetryLoading;
  const nextAction = activeView === "audit" ? auditAttempt?.recommended_next_action?.trim() || null : null;
  const renderedEntryCount = useMemo(
    () => renderedAttempt?.sections.reduce((total, section) => total + section.entries.length, 0) ?? 0,
    [renderedAttempt],
  );
  const metrics = useMemo(() => attemptMetrics(telemetryAttempt), [telemetryAttempt]);

  useEffect(() => {
    if (!open) {
      return;
    }
    setAutoScrollTelemetry(true);
  }, [open, selectedAttemptId]);

  useEffect(() => {
    if (!open || activeView !== "telemetry" || !autoScrollTelemetry) {
      return;
    }
    const scrollContainer = contentScrollRef.current;
    if (scrollContainer) {
      scrollContainer.scrollTop = scrollContainer.scrollHeight;
      return;
    }
    telemetryBottomRef.current?.scrollIntoView({ block: "end" });
  }, [activeView, autoScrollTelemetry, open, renderedEntryCount, selectedAttemptId]);

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
                <span className={cn("rounded-full border px-2 py-0.5 text-xs font-medium", statusTone(currentStatus))}>
                  {currentStatus.replace(/_/g, " ")}
                </span>
              </div>
              {nextAction ? <p className="text-sm text-muted-foreground">{nextAction}</p> : null}
            </div>
            <div className="flex items-center gap-2">
              {activeView === "telemetry" ? (
                <Button
                  variant={autoScrollTelemetry ? "default" : "outline"}
                  size="sm"
                  onClick={() => setAutoScrollTelemetry((enabled) => !enabled)}
                  aria-pressed={autoScrollTelemetry}
                >
                  {autoScrollTelemetry ? "Auto-scroll on" : "Auto-scroll off"}
                </Button>
              ) : null}
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
              { key: "metrics", label: "Metrics" },
              { key: "audit", label: "Audit history" },
            ].map((tab) => {
              const selected = activeView === tab.key;
              return (
                <button
                  key={tab.key}
                  type="button"
                  onClick={() => onViewChange(tab.key as ExecutionObservabilityView)}
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
                {waitingForNewAttempt ? (
                  <p className="rounded-xl border bg-background px-3 py-4 text-sm text-muted-foreground">
                    Waiting for the new attempt to start…
                  </p>
                ) : null}
                {!attempts.length && !waitingForNewAttempt ? (
                  <p className="rounded-xl border bg-background px-3 py-4 text-sm text-muted-foreground">
                    No attempts recorded for this step yet.
                  </p>
                ) : null}
                {attempts.map((attempt) => {
                  const selected = selectedAttemptId === attempt.attempt_id;
                  const displayStatus = displayStatusForAttempt(attempt, { latestNumber });
                  return (
                    <button
                      key={attempt.attempt_id}
                      type="button"
                      onClick={() => onSelectAttemptId(attempt.attempt_id)}
                      className={cn(
                        "w-full rounded-xl border bg-background px-3 py-3 text-left transition-colors",
                        selected ? "border-primary ring-1 ring-primary/20" : "hover:border-border",
                      )}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-sm font-semibold">Attempt {attempt.attempt_number}</span>
                        <span className={cn("rounded-full border px-2 py-0.5 text-[11px] font-medium", statusTone(displayStatus))}>
                          {statusLabel(displayStatus)}
                        </span>
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground" title={attempt.finished_at ? formatTimestamp(attempt.finished_at) : undefined}>
                        {displayStatus === "superseded"
                          ? "Superseded by a newer attempt"
                          : attempt.finished_at
                            ? formatTimeAgo(attempt.finished_at)
                            : attempt.started_at
                              ? "In progress"
                              : "Waiting to start"}
                      </p>
                    </button>
                  );
                })}
              </div>
            </div>

            <div ref={contentScrollRef} data-testid="execution-observability-content" className="min-h-0 overflow-y-auto px-6 py-4">
              {error ? <p className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</p> : null}
              {loading ? <p className="rounded-xl border px-4 py-3 text-sm text-muted-foreground">Loading…</p> : null}
              {!loading && !selectedPersistedAttempt && waitingForNewAttempt ? (
                <p className="rounded-xl border px-4 py-3 text-sm text-muted-foreground">
                  The new attempt will appear here as soon as it is persisted.
                </p>
              ) : null}
              {!loading && selectedPersistedAttempt ? (
                <div className="space-y-5">
                  <section className="rounded-xl border bg-muted/20 p-4">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-base font-semibold">Attempt {selectedPersistedAttempt.attempt_number}</span>
                      <span className={cn("rounded-full border px-2 py-0.5 text-xs font-medium", statusTone(selectedDisplayStatus))}>
                        {statusLabel(selectedDisplayStatus)}
                      </span>
                    </div>
                    <div className="mt-3 grid gap-3 text-sm md:grid-cols-2">
                      <div>
                        <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">Started</p>
                        <p className="mt-1">
                          {selectedPersistedAttempt.started_at ? formatTimeAgo(selectedPersistedAttempt.started_at) : "—"}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">Finished</p>
                        <p className="mt-1">
                          {selectedPersistedAttempt.finished_at ? formatTimeAgo(selectedPersistedAttempt.finished_at) : "—"}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">Duration</p>
                        <p className="mt-1">{formatDuration(renderedAttempt?.duration_ms ?? attemptDuration(selectedPersistedAttempt))}</p>
                      </div>
                      <div>
                        <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">
                          {activeView === "audit" ? "Next action" : "Live status"}
                        </p>
                        <p className="mt-1">
                          {activeView === "audit" ? nextAction || "—" : statusLabel(selectedDisplayStatus)}
                        </p>
                      </div>
                    </div>
                  </section>

                  {activeView === "telemetry" ? (
                    <section className="rounded-xl border bg-background p-4">
                      <div className="flex items-center justify-between gap-2">
                        <h3 className="text-sm font-semibold">Work units</h3>
                        <span className="text-xs text-muted-foreground">
                          {(selectedPersistedAttempt.work_units ?? []).length} unit
                          {(selectedPersistedAttempt.work_units ?? []).length === 1 ? "" : "s"}
                        </span>
                      </div>
                      {(selectedPersistedAttempt.work_units ?? []).length ? (
                        <div className="mt-3 space-y-2">
                          {(selectedPersistedAttempt.work_units ?? []).map((unit) => {
                            const latestUnitAttempt = [...(unit.attempts ?? [])].sort(
                              (left, right) => right.attempt_number - left.attempt_number,
                            )[0];
                            return (
                              <div key={`${unit.work_unit_id}-${latestUnitAttempt?.work_unit_attempt_id ?? "unit"}`} className="rounded-lg border bg-muted/20 px-3 py-2">
                                <div className="flex flex-wrap items-center justify-between gap-2">
                                  <div>
                                    <p className="text-sm font-medium">{unit.unit_key.replace(/\./g, " · ")}</p>
                                    <p className="text-xs text-muted-foreground">{unit.unit_kind.replace(/_/g, " ")}</p>
                                  </div>
                                  <span className={cn("rounded-full border px-2 py-0.5 text-xs", statusTone(unit.status))}>
                                    {statusLabel(unit.status)}
                                  </span>
                                </div>
                                {unit.error_message ? (
                                  <p className="mt-2 text-xs text-red-700">{unit.error_message}</p>
                                ) : null}
                                {latestUnitAttempt ? (
                                  <p className="mt-2 text-xs text-muted-foreground">
                                    Unit attempt {latestUnitAttempt.attempt_number}
                                    {latestUnitAttempt.finished_at ? ` · ${formatTimeAgo(latestUnitAttempt.finished_at)}` : ""}
                                  </p>
                                ) : null}
                              </div>
                            );
                          })}
                        </div>
                      ) : (
                        <p className="mt-3 text-sm text-muted-foreground">No work units have been recorded for this attempt yet.</p>
                      )}
                    </section>
                  ) : null}

                  {activeView === "metrics" ? (
                    metrics.length ? (
                      <section className="rounded-xl border bg-background p-4">
                        <div className="flex items-center justify-between gap-2">
                          <h3 className="text-sm font-semibold">Attempt metrics</h3>
                          <span className="text-xs text-muted-foreground">from live telemetry</span>
                        </div>
                        <div className="mt-3 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
                          {metrics.map((metric) => (
                            <div key={metric.label} className="rounded-lg border bg-muted/20 px-3 py-2">
                              <p className="text-[11px] font-medium tracking-[0.16em] text-muted-foreground uppercase">{metric.label}</p>
                              <p className="mt-1 font-semibold break-words">{metric.value}</p>
                            </div>
                          ))}
                        </div>
                      </section>
                    ) : (
                      <p className="rounded-xl border px-4 py-3 text-sm text-muted-foreground">
                        No attempt metrics have been recorded for this attempt yet.
                      </p>
                    )
                  ) : null}

                  {activeView !== "metrics" && !renderedAttempt?.sections.length ? (
                    <p className="rounded-xl border px-4 py-3 text-sm text-muted-foreground">
                      {activeView === "telemetry"
                        ? "No live telemetry has been recorded for this attempt yet."
                        : "No audit history has been recorded for this attempt yet."}
                    </p>
                  ) : null}

                  {activeView !== "metrics" ? renderedAttempt?.sections.map((section, sectionIndex) => (
                    <section key={`${section.kind}-${section.label}-${sectionIndex}`} className="space-y-3">
                      <div className="flex items-center justify-between gap-2">
                        <h3 className="text-sm font-semibold">{section.label}</h3>
                        <span className="text-xs text-muted-foreground">
                          {section.entries.length} entr{section.entries.length === 1 ? "y" : "ies"}
                        </span>
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
                                {entry.source_component ? <span className="text-xs text-muted-foreground">{entry.source_component}</span> : null}
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
                  )) : null}
                  {activeView === "telemetry" ? <div ref={telemetryBottomRef} data-testid="execution-observability-bottom" aria-hidden="true" /> : null}
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
