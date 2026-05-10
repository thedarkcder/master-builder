import type { WorkflowObservabilityEventRecord } from "@/lib/api";

export function workflowEventSequenceFromId(eventId: string): bigint | null {
  const match = /^telemetry:(\d+)$/.exec(eventId.trim());
  return match ? BigInt(match[1]) : null;
}

function workflowEventSequence(event: WorkflowObservabilityEventRecord): bigint | null {
  const idSequence = workflowEventSequenceFromId(event.event_id);
  if (idSequence !== null) {
    return idSequence;
  }
  if (typeof event.event_sequence === "string" && /^\d+$/.test(event.event_sequence.trim())) {
    return BigInt(event.event_sequence.trim());
  }
  if (typeof event.event_sequence === "number" && Number.isSafeInteger(event.event_sequence)) {
    return BigInt(event.event_sequence);
  }
  return null;
}

export function workflowEventSequenceCursor(events: { event_id: string }[]): string | null {
  let cursor: bigint | null = null;
  for (const event of events) {
    const sequence = workflowEventSequenceFromId(event.event_id);
    if (sequence !== null && (cursor === null || sequence > cursor)) {
      cursor = sequence;
    }
  }
  return cursor?.toString() ?? null;
}

export function compareWorkflowObservabilityEvents(
  left: WorkflowObservabilityEventRecord,
  right: WorkflowObservabilityEventRecord,
): number {
  const leftSequence = workflowEventSequence(left);
  const rightSequence = workflowEventSequence(right);
  if (leftSequence !== null && rightSequence !== null && leftSequence !== rightSequence) {
    return leftSequence < rightSequence ? -1 : 1;
  }

  const timeDelta = new Date(left.recorded_at).getTime() - new Date(right.recorded_at).getTime();
  if (timeDelta !== 0) {
    return timeDelta;
  }

  return left.event_id.localeCompare(right.event_id);
}
