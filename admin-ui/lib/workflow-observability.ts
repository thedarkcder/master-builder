import type {
  WorkflowObservabilityEventRecord,
  WorkflowOperationAttemptRecord,
  WorkflowStepAttemptTranscriptRecord,
  WorkflowTranscriptEntryRecord,
  WorkflowTranscriptSectionRecord,
} from "@/lib/api";
import { compareWorkflowObservabilityEvents } from "@/lib/workflow-event-order";

function sectionForEvent(event: WorkflowObservabilityEventRecord): WorkflowTranscriptSectionRecord["kind"] | null {
  const kind = String(event.event_kind || "").trim().toLowerCase();
  if (kind === "stage_request") return "prompts";
  if (kind === "stage_response") return "prompts";
  if (kind === "tool_request" || kind === "tool_result") return "tool_calls";
  if (kind.endsWith("_request")) return "external_requests";
  if (kind.endsWith("_response")) return "external_responses";
  if (
    kind === "workflow_operation_attempt_started" ||
    kind === "workflow_operation_attempt_retried" ||
    kind === "workflow_operation_attempt_completed" ||
    kind === "workflow_operation_attempt_waiting_for_input" ||
    kind === "workflow_operation_waiting_for_input"
  ) {
    return "summary";
  }
  if (kind === "workflow_operation_attempt_failed" || kind === "attempt_failed") return "outcome";
  if (
    kind === "stage_invocation_started" ||
    kind === "stage_invocation_finished" ||
    kind === "runtime_log" ||
    kind === "no_assistant_output_event" ||
    kind === "thread.started" ||
    kind === "turn.started" ||
    kind === "turn.completed" ||
    kind === "thread.completed"
  ) {
    return "runtime";
  }
  return null;
}

function titleForEvent(
  event: WorkflowObservabilityEventRecord,
  sectionKind: WorkflowTranscriptSectionRecord["kind"],
): string {
  const stageLabel = String(event.stage || event.payload?.stage || "").trim();
  const stagePrefix = stageLabel ? `${stageLabel.replace(/_/g, " ")} · ` : "";
  if (sectionKind === "prompts") {
    if (event.event_kind === "stage_request") return `${stagePrefix}Runtime request`;
    if (event.event_kind === "stage_response") return `${stagePrefix}Runtime response`;
  }
  if (sectionKind === "tool_calls") {
    const toolName = String(event.payload?.tool_name || "").trim();
    return `${stagePrefix}${toolName || event.event_kind.replace(/_/g, " ")}`;
  }
  if (sectionKind === "summary") return "Attempt lifecycle";
  if (sectionKind === "outcome") return String(event.level || "").toLowerCase() === "error" ? "Attempt failure" : "Attempt outcome";
  return `${stagePrefix}${event.event_kind.replace(/_/g, " ")}`;
}

function durationMs(attempt: WorkflowOperationAttemptRecord): number | null {
  if (!attempt.started_at || !attempt.finished_at) return null;
  return Math.max(0, new Date(attempt.finished_at).getTime() - new Date(attempt.started_at).getTime());
}

const SECTION_LABELS: Record<WorkflowTranscriptSectionRecord["kind"], string> = {
  summary: "Summary",
  runtime: "Runtime",
  prompts: "Prompts",
  tool_calls: "Tool calls",
  external_requests: "External requests",
  external_responses: "External responses",
  outcome: "Outcome",
};

const SECTION_ORDER: WorkflowTranscriptSectionRecord["kind"][] = [
  "summary",
  "runtime",
  "prompts",
  "tool_calls",
  "external_requests",
  "external_responses",
  "outcome",
];

function invocationGroupForEvent(event: WorkflowObservabilityEventRecord): { key: string; label: string } {
  const stage = String(event.stage || event.payload?.stage || "").trim();
  const invocationId = String(event.invocation_id || event.payload?.invocation_id || "").trim();
  const key = invocationId || stage || "runtime";
  const stageLabel = stage ? stage.replace(/_/g, " ") : "unscoped runtime";
  const suffix = invocationId ? ` · ${invocationId.slice(0, 8)}` : "";
  return { key, label: `Runtime · ${stageLabel}${suffix}` };
}

export function buildTelemetryAttemptView(
  attempt: WorkflowOperationAttemptRecord,
  events: WorkflowObservabilityEventRecord[],
): WorkflowStepAttemptTranscriptRecord {
  const sectionEntries: Record<Exclude<WorkflowTranscriptSectionRecord["kind"], "runtime">, WorkflowTranscriptEntryRecord[]> = {
    summary: [],
    prompts: [],
    tool_calls: [],
    external_requests: [],
    external_responses: [],
    outcome: [],
  };
  const runtimeSections = new Map<string, WorkflowTranscriptSectionRecord>();
  for (const event of events
    .filter((candidate) => candidate.attempt_id === attempt.attempt_id)
    .sort(compareWorkflowObservabilityEvents)) {
    const sectionKind = sectionForEvent(event);
    if (!sectionKind) continue;
    const entry = {
      entry_id: event.event_id,
      recorded_at: event.recorded_at,
      level: event.level,
      title: titleForEvent(event, sectionKind),
      message: event.message,
      source_component: event.source_component,
      payload: event.payload ?? {},
    };
    if (sectionKind === "runtime") {
      const group = invocationGroupForEvent(event);
      const section = runtimeSections.get(group.key) ?? {
        kind: "runtime" as const,
        label: group.label,
        entries: [],
      };
      section.entries.push(entry);
      runtimeSections.set(group.key, section);
      continue;
    }
    sectionEntries[sectionKind].push(entry);
  }
  const sections: WorkflowTranscriptSectionRecord[] = [];
  for (const kind of SECTION_ORDER) {
    if (kind === "runtime") {
      sections.push(...runtimeSections.values());
      continue;
    }
    if (sectionEntries[kind].length > 0) {
      sections.push({
        kind,
        label: SECTION_LABELS[kind],
        entries: sectionEntries[kind],
      });
    }
  }

  return {
    attempt_id: attempt.attempt_id,
    attempt_number: attempt.attempt_number,
    status: attempt.status,
    started_at: attempt.started_at,
    finished_at: attempt.finished_at,
    duration_ms: durationMs(attempt),
    error_category: attempt.error_category,
    failure_message: attempt.error_message,
    status_detail: attempt.status_detail,
    recommended_next_action: null,
    sections,
  };
}
