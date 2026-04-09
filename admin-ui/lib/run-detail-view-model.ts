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
