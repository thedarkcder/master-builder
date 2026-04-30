import type { WorkflowObservabilityEventRecord } from "@/lib/api";

export function workflowEventSequenceFromId(eventId: string): bigint | null {
  const match = /^telemetry:(\d+)$/.exec(eventId.trim());
  return match ? BigInt(match[1]) : null;
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
  const leftEventSequence = typeof left.event_sequence === "number" ? left.event_sequence : null;
  const rightEventSequence = typeof right.event_sequence === "number" ? right.event_sequence : null;
  if (leftEventSequence !== null && rightEventSequence !== null && leftEventSequence !== rightEventSequence) {
    return leftEventSequence - rightEventSequence;
  }

  const leftSequence = workflowEventSequenceFromId(left.event_id);
  const rightSequence = workflowEventSequenceFromId(right.event_id);
  if (leftSequence !== null && rightSequence !== null && leftSequence !== rightSequence) {
    return leftSequence < rightSequence ? -1 : 1;
  }

  const timeDelta = new Date(left.recorded_at).getTime() - new Date(right.recorded_at).getTime();
  if (timeDelta !== 0) {
    return timeDelta;
  }

  return left.event_id.localeCompare(right.event_id);
}
