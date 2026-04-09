import type { RunLogEventRecord, RunRecord } from "@/lib/api";
import { parseTelemetryPayload, stageColor } from "@/lib/run-detail-view-model";

export type InvocationSessionRow = {
  key: string;
  stage: string;
  attempt: number | null;
  invocationId: string;
  startedAt: string | null;
  finishedAt: string | null;
  status: string | null;
  durationMs: number | null;
  resumedSession: boolean | null;
  codexSessionId: string | null;
};

export type TimelineSegment = {
  key: string;
  label: string;
  stage: string;
  startMs: number;
  endMs: number;
  durationMs: number;
  color: string;
  detail: string;
};

export type RunTimelineView = {
  segments: TimelineSegment[];
  minStartMs: number;
  maxEndMs: number;
  totalMs: number;
  queueWaitMs: number;
  stageMs: number;
  resumedCount: number;
};

export function invocationStageFromCommand(command: string | null | undefined): string {
  const value = String(command ?? "").trim();
  if (!value) {
    return "unknown";
  }
  const idx = value.lastIndexOf(".");
  if (idx < 0 || idx === value.length - 1) {
    return value.toLowerCase();
  }
  return value.slice(idx + 1).trim().toLowerCase() || "unknown";
}

export function buildInvocationSessionRows(logs: RunLogEventRecord[]): InvocationSessionRow[] {
  const telemetryRows = logs
    .filter((entry) => entry.stage === "telemetry" && entry.stream === "system")
    .slice()
    .sort((a, b) => new Date(a.recorded_at).getTime() - new Date(b.recorded_at).getTime());
  const byInvocation = new Map<string, InvocationSessionRow>();
  for (const entry of telemetryRows) {
    const payload = parseTelemetryPayload(entry.message);
    if (!payload) {
      continue;
    }
    if (payload.event_kind !== "stage_invocation_started" && payload.event_kind !== "stage_invocation_finished") {
      continue;
    }
    const invocationId = String(entry.invocation_id ?? "").trim() || `unknown-${entry.recorded_at}-${entry.command ?? ""}`;
    const stage = invocationStageFromCommand(entry.command);
    const current =
      byInvocation.get(invocationId) ??
      ({
        key: invocationId,
        stage,
        attempt: entry.attempt ?? null,
        invocationId,
        startedAt: null,
        finishedAt: null,
        status: null,
        durationMs: null,
        resumedSession: null,
        codexSessionId: null
      } satisfies InvocationSessionRow);
    if (payload.event_kind === "stage_invocation_started") {
      current.startedAt = entry.recorded_at;
    }
    if (payload.event_kind === "stage_invocation_finished") {
      current.finishedAt = entry.recorded_at;
      current.status = payload.status ? String(payload.status) : current.status;
      current.durationMs = typeof payload.duration_ms === "number" ? payload.duration_ms : current.durationMs;
    }
    if (typeof payload.resumed_session === "boolean") {
      current.resumedSession = payload.resumed_session;
    }
    if (payload.codex_session_id) {
      current.codexSessionId = String(payload.codex_session_id);
    }
    byInvocation.set(invocationId, current);
  }
  return Array.from(byInvocation.values()).sort((a, b) => {
    const aTime = new Date(a.startedAt ?? a.finishedAt ?? 0).getTime();
    const bTime = new Date(b.startedAt ?? b.finishedAt ?? 0).getTime();
    return bTime - aTime;
  });
}

export function buildRunTimeline({
  run,
  logs,
  invocationSessionRows,
  isActiveRun,
}: {
  run: RunRecord | null;
  logs: RunLogEventRecord[];
  invocationSessionRows: InvocationSessionRow[];
  isActiveRun: boolean;
}): RunTimelineView | null {
  if (!run) {
    return null;
  }
  const telemetryRows = logs
    .filter((entry) => entry.stage === "telemetry" && entry.stream === "system")
    .slice()
    .sort((a, b) => new Date(a.recorded_at).getTime() - new Date(b.recorded_at).getTime());
  let queueWaitMs = 0;
  for (const entry of telemetryRows) {
    const payload = parseTelemetryPayload(entry.message);
    if (!payload || payload.event_kind !== "queue_wait") {
      continue;
    }
    if (typeof payload.queue_wait_ms === "number" && payload.queue_wait_ms >= 0) {
      queueWaitMs = payload.queue_wait_ms;
    }
  }
  const createdMs = run.created_at ? new Date(run.created_at).getTime() : Number.NaN;
  const startedMs = run.started_at ? new Date(run.started_at).getTime() : Number.NaN;
  const finishedMs = run.finished_at ? new Date(run.finished_at).getTime() : Number.NaN;
  const nowMs = Date.now();

  const segments: TimelineSegment[] = [];
  if (Number.isFinite(createdMs) && Number.isFinite(startedMs) && startedMs > createdMs) {
    const waitDurationMs = queueWaitMs > 0 ? queueWaitMs : startedMs - createdMs;
    const queueEnd = createdMs + waitDurationMs;
    segments.push({
      key: "queue_wait",
      label: "Queue Wait",
      stage: "queue_wait",
      startMs: createdMs,
      endMs: Math.max(createdMs + 1, queueEnd),
      durationMs: Math.max(1, waitDurationMs),
      color: stageColor("queue_wait"),
      detail: "Queued before task start",
    });
  }

  for (const row of invocationSessionRows) {
    if (!row.startedAt) {
      continue;
    }
    const startMs = new Date(row.startedAt).getTime();
    const endMs = row.finishedAt ? new Date(row.finishedAt).getTime() : (isActiveRun ? nowMs : Number.NaN);
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) {
      continue;
    }
    const inProgress = !row.finishedAt;
    segments.push({
      key: `stage-${row.invocationId}`,
      label: row.stage.toUpperCase(),
      stage: row.stage,
      startMs,
      endMs,
      durationMs: endMs - startMs,
      color: stageColor(row.stage),
      detail: `${inProgress ? "in progress" : row.resumedSession ? "resumed session" : "new session"}${row.attempt !== null ? ` · attempt ${row.attempt}` : ""}`,
    });
  }

  if (segments.length === 0) {
    return null;
  }
  const minStartMs = Math.min(...segments.map((segment) => segment.startMs));
  const maxEndMs = Math.max(
    ...segments.map((segment) => segment.endMs),
    Number.isFinite(finishedMs) ? finishedMs : 0,
    nowMs
  );
  const totalMs = Math.max(1, maxEndMs - minStartMs);
  const resumedCount = invocationSessionRows.filter((row) => row.resumedSession === true).length;
  const stageMs = segments
    .filter((segment) => segment.stage !== "queue_wait")
    .reduce((sum, segment) => sum + segment.durationMs, 0);
  return {
    segments: segments.sort((a, b) => a.startMs - b.startMs),
    minStartMs,
    maxEndMs,
    totalMs,
    queueWaitMs,
    stageMs,
    resumedCount,
  };
}
