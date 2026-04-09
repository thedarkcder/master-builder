import type { RunLogEventRecord, RunRecord } from "@/lib/api";
import { isRecord, type RunChatEntryKind, type WorkflowDiagnosticsView } from "@/lib/run-detail-view-model";
import { parseStageUpdateEvents } from "@/lib/run-detail-projections/diagnostics";

export type ChatTimelineEntry = {
  key: string;
  recordedAt: string;
  stage: string;
  attempt: number | null;
  speaker: string;
  text: string;
  kind: RunChatEntryKind;
};

function normalizeInlineText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
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
