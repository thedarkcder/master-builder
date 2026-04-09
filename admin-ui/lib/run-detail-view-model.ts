import type { RunLogEventRecord, RunRecord } from "@/lib/api";

export type InvocationTelemetry = {
  event_kind: string;
  status?: string;
  duration_ms?: number;
  resumed_session?: boolean;
  codex_session_id?: string;
  queue_wait_ms?: number;
  created_at?: string;
  started_at?: string;
};

export type TeamTaskProgressStatus = "not_started" | "running" | "completed" | "blocked";

export type WorkflowDiagnosticsHistoryEntry = {
  stage: string;
  attempt: string;
  event: string;
};

export type WorkflowDiagnosticsView = {
  stage: string;
  message: string;
  history: WorkflowDiagnosticsHistoryEntry[];
};

export type RunChatEntryKind = "message" | "reasoning" | "status" | "error";

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

export type ChatTimelineEntry = {
  key: string;
  recordedAt: string;
  stage: string;
  attempt: number | null;
  speaker: string;
  text: string;
  kind: RunChatEntryKind;
};

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function toStringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => String(item ?? "").trim()).filter((item) => item.length > 0);
}

export function isAbortLikeError(error: unknown): boolean {
  const message = (error as Error)?.message?.toLowerCase() ?? "";
  return message.includes("aborted");
}

export function statusFromLifecycleEvent(eventType: string): RunRecord["status"] | null {
  if (eventType === "TASK_COMPLETED") {
    return "succeeded";
  }
  if (eventType === "RUN_FAILED" || eventType === "TASK_FAILED") {
    return "failed";
  }
  return null;
}

export function parseTelemetryPayload(message: string): InvocationTelemetry | null {
  try {
    const payload = JSON.parse(message) as InvocationTelemetry;
    if (typeof payload !== "object" || payload === null) {
      return null;
    }
    const eventKind = String(payload.event_kind ?? "").trim();
    if (!eventKind) {
      return null;
    }
    return payload;
  } catch {
    return null;
  }
}

export function parseExecutionContext(plan: Record<string, unknown> | null | undefined): Record<string, string> {
  if (!isRecord(plan)) {
    return {};
  }
  const raw =
    (isRecord(plan["context"]) && isRecord(plan["context"]["execution_context"]))
      ? plan["context"]["execution_context"]
      : null;
  if (!isRecord(raw)) {
    return {};
  }
  const parsed: Record<string, string> = {};
  for (const key of [
    "execution_repo_dir",
    "workspace_key",
    "execution_branch",
    "integration_branch",
    "base_branch",
    "start_point_ref",
    "start_point_sha",
  ]) {
    const value = String(raw[key] ?? "").trim();
    if (value) {
      parsed[key] = value;
    }
  }
  return parsed;
}

export function formatDuration(durationMs: number): string {
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
  return `${seconds}s`;
}

export function formatTokenCount(value: number): string {
  const normalized = Math.max(0, Math.floor(value));
  if (normalized >= 1000_000) {
    return `${(normalized / 1000_000).toFixed(2)}m`;
  }
  if (normalized >= 1000) {
    return `${(normalized / 1000).toFixed(1)}k`;
  }
  return String(normalized);
}

export function stageColor(stage: string): string {
  const normalized = String(stage ?? "").trim().toLowerCase();
  if (normalized === "queue_wait") {
    return "#94a3b8";
  }
  if (normalized === "orchestrated_run") {
    return "#8b5cf6";
  }
  const palette = ["#0ea5e9", "#22c55e", "#f59e0b", "#ef4444", "#06b6d4", "#10b981", "#eab308", "#f97316", "#64748b"];
  let hash = 0;
  for (let idx = 0; idx < normalized.length; idx += 1) {
    hash = (hash * 31 + normalized.charCodeAt(idx)) >>> 0;
  }
  return palette[hash % palette.length];
}

export function stageDisplayLabel(stage: string): string {
  const normalized = String(stage ?? "").trim();
  if (!normalized) {
    return "UNKNOWN";
  }
  return normalized.replace(/_/g, " ").toUpperCase();
}

function normalizeInlineText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function logEntryIdentity(entry: RunLogEventRecord): string {
  return [
    entry.recorded_at,
    entry.stage,
    entry.stream,
    String(entry.invocation_id ?? "").trim(),
    String(entry.command ?? "").trim(),
    entry.message,
  ].join("::");
}

export function dedupeRunLogs(entries: RunLogEventRecord[]): RunLogEventRecord[] {
  const seen = new Set<string>();
  const ordered: RunLogEventRecord[] = [];
  for (const entry of entries) {
    const key = logEntryIdentity(entry);
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    ordered.push(entry);
  }
  return ordered;
}

export function parseRunLogChatText(
  entry: RunLogEventRecord,
): Pick<{ speaker: string; text: string; kind: RunChatEntryKind }, "speaker" | "text" | "kind"> | null {
  const raw = String(entry.message ?? "");
  const trimmed = raw.trim();
  if (!trimmed) {
    return null;
  }
  if (entry.stream === "stderr") {
    const lowered = trimmed.toLowerCase();
    if (
      lowered.includes("error") ||
      lowered.includes("failed") ||
      lowered.includes("fatal") ||
      lowered.includes("exception")
    ) {
      return { speaker: "runtime", text: trimmed, kind: "error" };
    }
  }

  let parsed: unknown = null;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    parsed = null;
  }
  if (!isRecord(parsed)) {
    return null;
  }

  const eventType = String(parsed.type ?? "").trim().toLowerCase();
  if (eventType === "turn.started") {
    return { speaker: "codex", text: "Turn started", kind: "status" };
  }
  if (eventType === "turn.completed") {
    const usage = isRecord(parsed.usage) ? parsed.usage : null;
    const inputTokens = usage ? String(usage.input_tokens ?? "").trim() : "";
    const outputTokens = usage ? String(usage.output_tokens ?? "").trim() : "";
    const usageSuffix = inputTokens || outputTokens ? ` (in: ${inputTokens || "?"}, out: ${outputTokens || "?"})` : "";
    return { speaker: "codex", text: `Turn completed${usageSuffix}`, kind: "status" };
  }

  const item = isRecord(parsed.item) ? parsed.item : null;
  if (eventType === "item.completed" && item) {
    const itemType = String(item.type ?? "").trim().toLowerCase();
    if (itemType === "agent_message") {
      return { speaker: "codex", text: String(item.text ?? "").trim(), kind: "message" };
    }
    if (itemType === "reasoning") {
      return { speaker: "codex", text: String(item.text ?? "").trim(), kind: "reasoning" };
    }
    if (itemType === "command_execution") {
      const status = String(item.status ?? "").trim().toLowerCase();
      const exitCode = item.exit_code;
      if (status === "failed" || (typeof exitCode === "number" && exitCode !== 0)) {
        const command = normalizeInlineText(String(item.command ?? ""));
        const output = normalizeInlineText(String(item.aggregated_output ?? ""));
        return {
          speaker: "command",
          text: `Command failed${typeof exitCode === "number" ? ` (exit ${exitCode})` : ""}: ${command}${output ? ` | ${output}` : ""}`,
          kind: "error",
        };
      }
    }
  }

  return null;
}

export function normalizeTeamTaskStatus(status: string | null | undefined): TeamTaskProgressStatus {
  const normalized = String(status ?? "").trim().toLowerCase();
  if (normalized === "completed" || normalized === "succeeded" || normalized === "approved") {
    return "completed";
  }
  if (normalized === "running" || normalized === "in_progress" || normalized === "active") {
    return "running";
  }
  if (normalized === "blocked" || normalized === "failed" || normalized === "awaiting_approval") {
    return "blocked";
  }
  return "not_started";
}

export function teamTaskStatusColor(status: TeamTaskProgressStatus): string {
  switch (status) {
    case "completed":
      return "#22c55e";
    case "running":
      return "#0ea5e9";
    case "blocked":
      return "#ef4444";
    default:
      return "#94a3b8";
  }
}

export function teamTaskStatusLabel(status: TeamTaskProgressStatus): string {
  switch (status) {
    case "completed":
      return "completed";
    case "running":
      return "running";
    case "blocked":
      return "blocked";
    default:
      return "pending";
  }
}

function stageUpdatesFromPlan(plan: Record<string, unknown> | null | undefined): Array<Record<string, unknown>> {
  if (!isRecord(plan)) {
    return [];
  }
  const eventsRoot = isRecord(plan["events"]) ? plan["events"] : null;
  if (!isRecord(eventsRoot)) {
    return [];
  }
  return Array.isArray(eventsRoot["stage_updates"])
    ? (eventsRoot["stage_updates"] as Array<Record<string, unknown>>)
    : [];
}

export function parseWorkflowDiagnostics(run: RunRecord | null): WorkflowDiagnosticsView | null {
  if (!run) {
    return null;
  }
  const teamRuntimeState = isRecord(run.team_run?.runtime_state) ? run.team_run?.runtime_state : null;
  const historyRaw = Array.isArray(teamRuntimeState?.["history"]) ? teamRuntimeState["history"] : [];
  const history: WorkflowDiagnosticsHistoryEntry[] = historyRaw
    .map((entry) => {
      if (!isRecord(entry)) {
        return null;
      }
      return {
        stage: String(entry["stage"] ?? "").trim(),
        attempt: String(entry["attempt"] ?? "").trim(),
        event: String(entry["event"] ?? "").trim(),
      };
    })
    .filter((entry): entry is WorkflowDiagnosticsHistoryEntry => Boolean(entry && entry.event));

  const stageTraceRaw = isRecord(run.plan) && isRecord(run.plan["events"]) && Array.isArray(run.plan["events"]["stage_trace"])
    ? (run.plan["events"]["stage_trace"] as Array<Record<string, unknown>>)
    : [];
  const stageFromTrace = [...stageTraceRaw]
    .reverse()
    .map((entry) => String(entry["stage"] ?? "").trim())
    .find((stage) => stage.length > 0) ?? "";
  const stageFromHistory = [...history]
    .reverse()
    .map((entry) => entry.stage)
    .find((stage) => stage.length > 0) ?? "";
  const stage = stageFromHistory || stageFromTrace || "workflow";

  const blockerMessage = isRecord(run.plan) && isRecord(run.plan["workflow"])
    ? String(run.plan["workflow"]["blocker_message"] ?? "").trim()
    : "";
  const message = String(run.last_error ?? "").trim() || blockerMessage;
  if (!message && history.length === 0) {
    return null;
  }

  return {
    stage,
    message,
    history,
  };
}

export function parseStageUpdateEvents(run: RunRecord | null): Array<Record<string, unknown>> {
  if (!run || !isRecord(run.plan)) {
    return [];
  }
  return stageUpdatesFromPlan(run.plan);
}

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

export function buildChatTimelineEntries({
  run,
  logs,
  workflowDiagnostics,
}: {
  run: RunRecord | null;
  logs: RunLogEventRecord[];
  workflowDiagnostics: WorkflowDiagnosticsView | null;
}): ChatTimelineEntry[] {
  const stageUpdates = parseStageUpdateEvents(run);
  const stageUpdateEntries: ChatTimelineEntry[] = stageUpdates
    .map((entry, idx): ChatTimelineEntry | null => {
      if (!isRecord(entry)) {
        return null;
      }
      const stage = String(entry["stage"] ?? "").trim() || "workflow";
      const rawMessage =
        String(entry["jira_message"] ?? "").trim() || String(entry["discord_message"] ?? "").trim();
      if (!rawMessage) {
        return null;
      }
      return {
        key: `stage-update-${idx}`,
        recordedAt: run?.finished_at ?? run?.started_at ?? run?.created_at ?? new Date().toISOString(),
        stage,
        attempt: null,
        speaker: "system",
        text: `Stage update: ${stage}. ${rawMessage}`,
        kind: "status"
      } satisfies ChatTimelineEntry;
    })
    .filter((entry): entry is ChatTimelineEntry => entry !== null);

  const logEntries = logs
    .slice()
    .sort((a, b) => new Date(a.recorded_at).getTime() - new Date(b.recorded_at).getTime())
    .map((entry, idx): ChatTimelineEntry | null => {
      const parsed = parseRunLogChatText(entry);
      if (!parsed) {
        return null;
      }
      return {
        key: `${entry.recorded_at}-${entry.stage}-${idx}`,
        recordedAt: entry.recorded_at,
        stage: entry.stage,
        attempt: entry.attempt,
        speaker: parsed.speaker,
        text: parsed.text,
        kind: parsed.kind
      } satisfies ChatTimelineEntry;
    })
    .filter((entry): entry is ChatTimelineEntry => entry !== null);

  const diagnosticsEntries: ChatTimelineEntry[] = (workflowDiagnostics?.history ?? []).map((entry, idx) => ({
    key: `diag-${idx}`,
    recordedAt: run?.finished_at ?? run?.started_at ?? run?.created_at ?? new Date().toISOString(),
    stage: entry.stage || "workflow",
    attempt: Number.isFinite(Number(entry.attempt)) ? Number(entry.attempt) : null,
    speaker: "diagnostics",
    text: entry.event,
    kind: "status"
  }));

  const timeline = [...logEntries, ...diagnosticsEntries, ...stageUpdateEntries]
    .map((entry, index) => ({ entry, index }))
    .sort((a, b) => {
      const tsDiff = new Date(a.entry.recordedAt).getTime() - new Date(b.entry.recordedAt).getTime();
      if (tsDiff !== 0) {
        return tsDiff;
      }
      return a.index - b.index;
    })
    .slice(-160)
    .map((wrapped) => wrapped.entry);
  const seen = new Set<string>();
  return timeline.filter((entry) => {
    const key = [
      entry.recordedAt,
      entry.stage,
      entry.attempt ?? "",
      entry.speaker,
      entry.kind,
      entry.text
    ].join("::");
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}
