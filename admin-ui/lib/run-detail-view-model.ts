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

export type StageCheckpointEntry = {
  status: string;
  completedAt: string | null;
  summary: string;
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

export function parseStageCheckpoints(
  plan: Record<string, unknown> | null | undefined,
): Partial<Record<string, StageCheckpointEntry>> {
  if (!isRecord(plan)) {
    return {};
  }
  const stagesRaw = isRecord(plan["stages"]) ? plan["stages"] : null;
  if (!stagesRaw) {
    return {};
  }
  const parsed: Partial<Record<string, StageCheckpointEntry>> = {};
  for (const stage of ["pm", "dev", "test", "review"]) {
    const item = stagesRaw[stage];
    if (!isRecord(item)) {
      continue;
    }
    parsed[stage] = {
      status: String(item["status"] ?? "").trim().toLowerCase() || "completed",
      completedAt: String(item["completed_at"] ?? "").trim() || null,
      summary: String(item["summary"] ?? "").trim(),
    };
  }
  return parsed;
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

export function parseStageArtifact(
  plan: Record<string, unknown> | null | undefined,
  stage: string,
): Record<string, unknown> | null {
  if (!isRecord(plan)) {
    return null;
  }
  const stagesRaw = isRecord(plan["stages"]) ? plan["stages"] : null;
  if (!stagesRaw) {
    return null;
  }
  const stageRaw = stagesRaw[stage];
  if (!isRecord(stageRaw)) {
    return null;
  }
  return isRecord(stageRaw["artifact"]) ? stageRaw["artifact"] : null;
}

export function stageFromCommand(command: string | null | undefined): string {
  const value = String(command ?? "").trim();
  if (!value) {
    return "unknown";
  }
  const idx = value.lastIndexOf(".");
  if (idx < 0 || idx === value.length - 1) {
    return value;
  }
  return value.slice(idx + 1);
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
  switch (stage) {
    case "queue_wait":
      return "#94a3b8";
    case "pm":
      return "#0ea5e9";
    case "dev":
      return "#22c55e";
    case "test":
      return "#f59e0b";
    case "review":
      return "#ef4444";
    case "orchestrated_run":
      return "#8b5cf6";
    default:
      return "#64748b";
  }
}

export function stageDisplayLabel(stage: string): string {
  if (stage === "pm" || stage === "dev" || stage === "test" || stage === "review") {
    return stage.toUpperCase();
  }
  if (stage === "orchestrated_run") {
    return "ORCHESTRATED RUN";
  }
  return stage;
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
