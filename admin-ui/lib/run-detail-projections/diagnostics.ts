import type { RunRecord } from "@/lib/api";
import { isRecord, type WorkflowDiagnosticsHistoryEntry, type WorkflowDiagnosticsView } from "@/lib/run-detail-view-model";

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
