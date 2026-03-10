import type { RunLogEventRecord } from "@/lib/api";

export type InvocationTelemetry = {
  event_kind: string;
  status?: string;
  duration_ms?: number;
  resumed_session?: boolean;
  codex_session_id?: string;
  queue_wait_ms?: number;
  created_at?: string;
  started_at?: string;
  error?: string;
};

export type ParsedChatLog = {
  speaker: string;
  text: string;
  kind: "message" | "reasoning" | "status" | "error";
};

export type WorkflowDiagnosticsFallback = {
  stage: string;
  message: string;
  history: Array<{
    stage: string;
    attempt: string;
    event: string;
  }>;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function clipText(value: string, maxChars = 280): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (normalized.length <= maxChars) {
    return normalized;
  }
  return `${normalized.slice(0, maxChars - 1).trimEnd()}…`;
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

export function parseRunLogChatText(entry: RunLogEventRecord): ParsedChatLog | null {
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
      return { speaker: "runtime", text: clipText(trimmed), kind: "error" };
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
  if (eventType === "error") {
    const error = isRecord(parsed.error) ? parsed.error : null;
    const message = String(error?.message ?? parsed.message ?? "").trim() || trimmed;
    return { speaker: "codex", text: clipText(message), kind: "error" };
  }
  if (eventType === "turn.failed") {
    const error = isRecord(parsed.error) ? parsed.error : null;
    const message = String(error?.message ?? parsed.message ?? "").trim();
    const text = message ? `Turn failed: ${message}` : "Turn failed";
    return { speaker: "codex", text: clipText(text), kind: "error" };
  }

  const item = isRecord(parsed.item) ? parsed.item : null;
  if (eventType === "item.completed" && item) {
    const itemType = String(item.type ?? "").trim().toLowerCase();
    if (itemType === "agent_message") {
      return { speaker: "codex", text: clipText(String(item.text ?? "")), kind: "message" };
    }
    if (itemType === "reasoning") {
      return { speaker: "codex", text: clipText(String(item.text ?? "")), kind: "reasoning" };
    }
    if (itemType === "command_execution") {
      const status = String(item.status ?? "").trim().toLowerCase();
      const exitCode = item.exit_code;
      if (status === "failed" || (typeof exitCode === "number" && exitCode !== 0)) {
        const command = clipText(String(item.command ?? ""), 160);
        const output = clipText(String(item.aggregated_output ?? ""), 200);
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

export function deriveTelemetryDiagnostics(logs: RunLogEventRecord[]): WorkflowDiagnosticsFallback | null {
  const telemetryRows = logs
    .filter((entry) => entry.stage === "telemetry" && entry.stream === "system")
    .slice()
    .sort((a, b) => new Date(b.recorded_at).getTime() - new Date(a.recorded_at).getTime());
  for (const entry of telemetryRows) {
    const payload = parseTelemetryPayload(entry.message);
    if (!payload || payload.event_kind !== "stage_invocation_finished") {
      continue;
    }
    if (String(payload.status ?? "").trim().toLowerCase() !== "failed") {
      continue;
    }
    const message = String(payload.error ?? "").trim();
    if (!message) {
      continue;
    }
    const stage = stageFromCommand(entry.command);
    return {
      stage,
      message,
      history: [
        {
          stage,
          attempt: entry.attempt === null ? "" : String(entry.attempt),
          event: message,
        },
      ],
    };
  }
  return null;
}
