"use client";

import { useEffect, useRef } from "react";

import {
  streamRunEvents,
  type Credentials,
  type RunEventRecord,
  type RunStatus,
  type RuntimeLogEventRecord,
} from "@/lib/api";

type RuntimeLogStreamEvent = RuntimeLogEventRecord & { event_kind?: string };
type RunStreamEvent = RunEventRecord | RuntimeLogStreamEvent;

type RunEventStreamCallbacks = {
  onLogEvent: (event: RuntimeLogEventRecord) => void;
  onLifecycleEvent: (event: RunEventRecord) => void;
  onStatusChange: (status: RunStatus) => void;
  onError: (message: string) => void;
};

export type UseRunEventStreamParams = RunEventStreamCallbacks & {
  credentials: Credentials | null;
  runId: string | null;
  runStatus: RunStatus | null;
};

function isAbortLikeError(error: unknown): boolean {
  const message = (error as Error)?.message?.toLowerCase() ?? "";
  return message.includes("aborted");
}

function isRuntimeLogStreamEvent(event: RunStreamEvent): event is RuntimeLogStreamEvent {
  return (event as { event_kind?: string }).event_kind === "run_log" || "message" in event;
}

function statusFromLifecycleEvent(eventType: string): RunStatus | null {
  if (eventType === "TASK_COMPLETED") {
    return "succeeded";
  }
  if (eventType === "RUN_FAILED" || eventType === "TASK_FAILED") {
    return "failed";
  }
  return null;
}

export function useRunEventStream({
  credentials,
  runId,
  runStatus,
  onLogEvent,
  onLifecycleEvent,
  onStatusChange,
  onError,
}: UseRunEventStreamParams): void {
  const callbacksRef = useRef<RunEventStreamCallbacks>({
    onLogEvent,
    onLifecycleEvent,
    onStatusChange,
    onError,
  });

  useEffect(() => {
    callbacksRef.current = {
      onLogEvent,
      onLifecycleEvent,
      onStatusChange,
      onError,
    };
  }, [onLogEvent, onLifecycleEvent, onStatusChange, onError]);

  useEffect(() => {
    if (!runId || !runStatus || !credentials) {
      return;
    }
    if (runStatus !== "queued" && runStatus !== "running") {
      return;
    }
    const controller = new AbortController();
    void streamRunEvents(
      credentials,
      runId,
      (event) => {
        if (isRuntimeLogStreamEvent(event)) {
          callbacksRef.current.onLogEvent(event);
          return;
        }

        callbacksRef.current.onLifecycleEvent(event);
        const nextStatus = statusFromLifecycleEvent(event.event_type);
        if (nextStatus) {
          callbacksRef.current.onStatusChange(nextStatus);
        }
      },
      controller.signal,
    ).catch((error) => {
      if (controller.signal.aborted || isAbortLikeError(error)) {
        return;
      }
      callbacksRef.current.onError((error as Error).message);
    });
    return () => {
      controller.abort();
    };
  }, [credentials, runId, runStatus]);
}
