"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  cancelRun,
  getRun,
  listRunEvents,
  listRunLogs,
  rerunRun,
  streamRunEvents,
  type RunEventRecord,
  type RunLogEventRecord,
  type RunRecord
} from "@/lib/api";

type InvocationTelemetry = {
  event_kind: string;
  status?: string;
  duration_ms?: number;
  resumed_session?: boolean;
  codex_session_id?: string;
  queue_wait_ms?: number;
  created_at?: string;
  started_at?: string;
};

type InvocationSessionRow = {
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

type TimelineSegment = {
  key: string;
  label: string;
  stage: string;
  startMs: number;
  endMs: number;
  durationMs: number;
  color: string;
  detail: string;
};

type WorkflowDiagnosticsHistoryEntry = {
  stage: string;
  attempt: string;
  event: string;
};

type ChatTimelineEntry = {
  key: string;
  recordedAt: string;
  stage: string;
  attempt: number | null;
  speaker: string;
  text: string;
  kind: "message" | "reasoning" | "status" | "error";
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function toStringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => String(item ?? "").trim()).filter((item) => item.length > 0);
}

function isAbortLikeError(error: unknown): boolean {
  const message = (error as Error)?.message?.toLowerCase() ?? "";
  return message.includes("aborted");
}

function statusFromLifecycleEvent(eventType: string): RunRecord["status"] | null {
  if (eventType === "TASK_COMPLETED") {
    return "succeeded";
  }
  if (eventType === "RUN_FAILED" || eventType === "TASK_FAILED") {
    return "failed";
  }
  return null;
}

function statusBadge(status: string) {
  if (status === "succeeded") {
    return <Badge>{status}</Badge>;
  }
  if (status === "failed" || status === "blocked") {
    return <Badge variant="secondary">{status}</Badge>;
  }
  return <Badge variant="outline">{status}</Badge>;
}

function parseTelemetryPayload(message: string): InvocationTelemetry | null {
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

function stageFromCommand(command: string | null | undefined): string {
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

function formatDuration(durationMs: number): string {
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

function stageColor(stage: string): string {
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
    default:
      return "#64748b";
  }
}

function stageDisplayLabel(stage: string): string {
  if (stage === "pm" || stage === "dev" || stage === "test" || stage === "review") {
    return stage.toUpperCase();
  }
  return stage;
}

function clipText(value: string, maxChars = 280): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (normalized.length <= maxChars) {
    return normalized;
  }
  return `${normalized.slice(0, maxChars - 1).trimEnd()}…`;
}

function parseRunLogChatText(entry: RunLogEventRecord): Pick<ChatTimelineEntry, "speaker" | "text" | "kind"> | null {
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
          kind: "error"
        };
      }
    }
  }

  return null;
}

export default function RunDetailPage() {
  const params = useParams<{ runId: string }>();
  const { credentials, ready } = useAuth();
  const [run, setRun] = useState<RunRecord | null>(null);
  const [events, setEvents] = useState<RunEventRecord[]>([]);
  const [logs, setLogs] = useState<RunLogEventRecord[]>([]);
  const [busy, setBusy] = useState(false);
  const [rerunBusy, setRerunBusy] = useState(false);
  const [forceRerunBusy, setForceRerunBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [logAgentFilter, setLogAgentFilter] = useState("all");
  const [logStageFilter, setLogStageFilter] = useState("all");
  const [logStreamFilter, setLogStreamFilter] = useState("all");
  const [loadingOlderLogs, setLoadingOlderLogs] = useState(false);
  const [hasMoreLogs, setHasMoreLogs] = useState(false);

  const loadRun = useCallback(async () => {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      const [payload, runEvents, runLogs] = await Promise.all([
        getRun(credentials, params.runId),
        listRunEvents(credentials, params.runId, { limit: 200 }),
        listRunLogs(credentials, params.runId, { limit: 200 })
      ]);
      setRun(payload);
      setEvents(runEvents);
      setLogs(runLogs);
      setHasMoreLogs(runLogs.length >= 200);
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to load run: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }, [credentials, params.runId]);

  useEffect(() => {
    if (ready && credentials) {
      void loadRun();
    }
  }, [ready, credentials, loadRun]);

  useEffect(() => {
    if (!run || !credentials) {
      return;
    }
    if (run.status !== "queued" && run.status !== "running") {
      return;
    }
    const controller = new AbortController();
    void streamRunEvents(
      credentials,
      params.runId,
      (event) => {
        if ((event as { event_kind?: string }).event_kind === "run_log" || "message" in event) {
          const logEvent = event as RunLogEventRecord;
          setLogs((prev) => {
            if (
              prev.some(
                (entry) =>
                  entry.recorded_at === logEvent.recorded_at &&
                  entry.stage === logEvent.stage &&
                  entry.stream === logEvent.stream &&
                  entry.message === logEvent.message
              )
            ) {
              return prev;
            }
            const next = [...prev, logEvent];
            return next.slice(-800);
          });
        } else {
          const lifecycleEvent = event as RunEventRecord;
          setEvents((prev) => {
            if (
              prev.some(
                (entry) =>
                  entry.event_type === lifecycleEvent.event_type &&
                  entry.recorded_at === lifecycleEvent.recorded_at &&
                  entry.agent_id === lifecycleEvent.agent_id
              )
            ) {
              return prev;
            }
            const next = [...prev, lifecycleEvent];
            return next.slice(-200);
          });
          const nextStatus = statusFromLifecycleEvent(lifecycleEvent.event_type);
          if (nextStatus) {
            setRun((prev) => (prev ? { ...prev, status: nextStatus } : prev));
          }
        }
      },
      controller.signal
    ).catch((error) => {
      if (controller.signal.aborted || isAbortLikeError(error)) {
        return;
      }
      setStatusLine(`Run event stream closed: ${(error as Error).message}`);
    });
    return () => {
      controller.abort();
    };
  }, [run, credentials, params.runId, loadRun]);

  async function handleRerun() {
    if (!credentials || !run) {
      return;
    }
    setRerunBusy(true);
    try {
      const nextRun = await rerunRun(credentials, run.run_id);
      setStatusLine(`Queued rerun ${nextRun.run_id} for ${nextRun.issue_key}.`);
      window.location.href = `/runs/${encodeURIComponent(nextRun.run_id)}`;
    } catch (error) {
      setStatusLine(`Failed to rerun: ${(error as Error).message}`);
    } finally {
      setRerunBusy(false);
    }
  }

  async function handleForceRerun() {
    if (!credentials || !run) {
      return;
    }
    setForceRerunBusy(true);
    try {
      const cancelled = await cancelRun(credentials, run.run_id);
      const nextRun = await rerunRun(credentials, cancelled.run_id);
      setStatusLine(`Force-cancelled ${cancelled.run_id} and queued rerun ${nextRun.run_id}.`);
      window.location.href = `/runs/${encodeURIComponent(nextRun.run_id)}`;
    } catch (error) {
      setStatusLine(`Failed to force rerun: ${(error as Error).message}`);
    } finally {
      setForceRerunBusy(false);
    }
  }

  async function handleLoadOlderLogs() {
    if (!credentials || logs.length === 0 || loadingOlderLogs || !hasMoreLogs) {
      return;
    }
    const oldest = logs[logs.length - 1];
    setLoadingOlderLogs(true);
    try {
      const olderLogs = await listRunLogs(credentials, params.runId, {
        limit: 200,
        beforeRecordedAt: oldest.recorded_at
      });
      setLogs((prev) => {
        const dedupe = new Set(prev.map((entry) => `${entry.recorded_at}:${entry.stage}:${entry.stream}:${entry.message}`));
        const merged = [...prev];
        for (const candidate of olderLogs) {
          const key = `${candidate.recorded_at}:${candidate.stage}:${candidate.stream}:${candidate.message}`;
          if (dedupe.has(key)) {
            continue;
          }
          dedupe.add(key);
          merged.push(candidate);
        }
        return merged;
      });
      setHasMoreLogs(olderLogs.length >= 200);
    } catch (error) {
      setStatusLine(`Failed to load older logs: ${(error as Error).message}`);
    } finally {
      setLoadingOlderLogs(false);
    }
  }

  const isRerunnable = Boolean(run);
  const isActiveRun = run?.status === "queued" || run?.status === "running";
  const liveStageUpdates = Array.isArray(run?.plan?.["live_stage_updates"])
    ? (run?.plan?.["live_stage_updates"] as Array<Record<string, unknown>>)
    : [];
  function selectedAgentStage(filter: string): string {
    if (filter === "tester") {
      return "test";
    }
    return filter;
  }
  const filteredLogs = useMemo(
    () =>
      logs.filter((entry) => {
        const agentMatch =
          logAgentFilter === "all" || entry.stage === selectedAgentStage(logAgentFilter);
        const stageMatch = logStageFilter === "all" || entry.stage === logStageFilter;
        const streamMatch = logStreamFilter === "all" || entry.stream === logStreamFilter;
        return agentMatch && stageMatch && streamMatch;
      }),
    [logs, logAgentFilter, logStageFilter, logStreamFilter]
  );
  const invocationSessionRows = useMemo(() => {
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
      const stage = stageFromCommand(entry.command);
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
  }, [logs]);
  const latestCodexSessionId = useMemo(() => {
    const fromTimeline = invocationSessionRows.find((row) => row.codexSessionId)?.codexSessionId;
    if (fromTimeline) {
      return fromTimeline;
    }
    return run?.dev_session_id ?? null;
  }, [invocationSessionRows, run?.dev_session_id]);
  const workflowDiagnostics = useMemo(() => {
    if (!isRecord(run?.plan)) {
      return null;
    }
    const diagnosticsRaw = run.plan["diagnostics"];
    if (!isRecord(diagnosticsRaw)) {
      return null;
    }
    const historyRaw = Array.isArray(diagnosticsRaw["history"]) ? diagnosticsRaw["history"] : [];
    const history: WorkflowDiagnosticsHistoryEntry[] = historyRaw
      .map((entry) => {
        if (!isRecord(entry)) {
          return null;
        }
        return {
          stage: String(entry["stage"] ?? "").trim(),
          attempt: String(entry["attempt"] ?? "").trim(),
          event: String(entry["event"] ?? "").trim()
        };
      })
      .filter((entry): entry is WorkflowDiagnosticsHistoryEntry => Boolean(entry && entry.event));
    return {
      stage: String(diagnosticsRaw["stage"] ?? "").trim(),
      message: String(diagnosticsRaw["message"] ?? "").trim(),
      history
    };
  }, [run?.plan]);
  const agentOutcomes = useMemo(() => {
    if (!isRecord(run?.plan)) {
      return [];
    }
    const planRoot = run.plan;
    const workflowPlan = isRecord(planRoot["plan"]) ? planRoot["plan"] : null;
    const pmItems = [
      ...toStringList(workflowPlan?.["plan_steps"]),
      ...toStringList(workflowPlan?.["acceptance_criteria"]).map((item) => `AC: ${item}`),
      ...toStringList(workflowPlan?.["risks"]).map((item) => `Risk: ${item}`)
    ];
    const nextStage = workflowPlan ? String(workflowPlan["next_stage"] ?? "").trim() : "";
    const executionWorker = workflowPlan ? String(workflowPlan["execution_worker_capability"] ?? "").trim() : "";
    if (nextStage) {
      pmItems.push(`Next stage: ${nextStage}`);
    }
    if (executionWorker) {
      pmItems.push(`Execution worker: ${executionWorker}`);
    }
    const reviewHistory = (workflowDiagnostics?.history ?? [])
      .filter((entry) => entry.stage.toLowerCase() === "review")
      .map((entry) => entry.event);
    return [
      {
        stage: "pm",
        label: "PM",
        items: pmItems,
        feedback: null as string | null,
        emptyText: "No PM output captured."
      },
      {
        stage: "dev",
        label: "Dev",
        items: toStringList(planRoot["dev_rationale"]),
        feedback: null as string | null,
        emptyText: "No Dev rationale captured."
      },
      {
        stage: "test",
        label: "Test",
        items: toStringList(planRoot["test_guidance"]),
        feedback: null as string | null,
        emptyText: "No test guidance captured."
      },
      {
        stage: "review",
        label: "Review",
        items: [...toStringList(planRoot["review_summary"]), ...reviewHistory],
        feedback: String(planRoot["review_feedback"] ?? "").trim() || null,
        emptyText: "No review summary captured."
      }
    ];
  }, [run?.plan, workflowDiagnostics?.history]);
  const runTimeline = useMemo(() => {
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
    const createdMs = run.created_at ? new Date(run.created_at).getTime() : NaN;
    const startedMs = run.started_at ? new Date(run.started_at).getTime() : NaN;
    const finishedMs = run.finished_at ? new Date(run.finished_at).getTime() : NaN;
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
      if (!row.startedAt || !row.finishedAt) {
        continue;
      }
      const startMs = new Date(row.startedAt).getTime();
      const endMs = new Date(row.finishedAt).getTime();
      if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) {
        continue;
      }
      segments.push({
        key: `stage-${row.invocationId}`,
        label: row.stage.toUpperCase(),
        stage: row.stage,
        startMs,
        endMs,
        durationMs: endMs - startMs,
        color: stageColor(row.stage),
        detail: `${row.resumedSession ? "resumed session" : "new session"}${row.attempt !== null ? ` · attempt ${row.attempt}` : ""}`,
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
  }, [invocationSessionRows, logs, run]);
  const chatTimelineEntries = useMemo(() => {
    const stageUpdates = isRecord(run?.plan) && Array.isArray(run.plan["stage_updates"]) ? run.plan["stage_updates"] : [];
    const stageUpdateEntries: ChatTimelineEntry[] = stageUpdates
      .map((entry, idx) => {
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
          text: `Stage update: ${stage}. ${clipText(rawMessage, 220)}`,
          kind: "status"
        } satisfies ChatTimelineEntry;
      })
      .filter((entry): entry is ChatTimelineEntry => Boolean(entry));

    const logEntries = logs
      .slice()
      .sort((a, b) => new Date(a.recorded_at).getTime() - new Date(b.recorded_at).getTime())
      .map((entry, idx) => {
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
      .filter((entry): entry is ChatTimelineEntry => Boolean(entry));

    const diagnosticsEntries: ChatTimelineEntry[] = (workflowDiagnostics?.history ?? []).map((entry, idx) => ({
      key: `diag-${idx}`,
      recordedAt: run?.finished_at ?? run?.started_at ?? run?.created_at ?? new Date().toISOString(),
      stage: entry.stage || "workflow",
      attempt: Number.isFinite(Number(entry.attempt)) ? Number(entry.attempt) : null,
      speaker: "diagnostics",
      text: clipText(entry.event, 320),
      kind: entry.stage.toLowerCase() === "review" ? "error" : "status"
    }));

    return [...logEntries, ...diagnosticsEntries, ...stageUpdateEntries]
      .sort((a, b) => new Date(a.recordedAt).getTime() - new Date(b.recordedAt).getTime())
      .slice(-160);
  }, [logs, run?.created_at, run?.finished_at, run?.plan, run?.started_at, workflowDiagnostics?.history]);
  const codePath = useMemo(() => {
    for (let idx = logs.length - 1; idx >= 0; idx -= 1) {
      const value = logs[idx]?.working_dir?.trim();
      if (value) {
        return value;
      }
    }
    return null;
  }, [logs]);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle>{run?.run_id ?? params.runId}</CardTitle>
            <CardDescription>Run details</CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" onClick={() => void loadRun()} disabled={busy}>
              {busy ? "Refreshing..." : "Refresh"}
            </Button>
            {isRerunnable ? (
              <Button variant="default" onClick={() => void handleRerun()} disabled={rerunBusy}>
                {rerunBusy ? "Requeueing..." : "Rerun"}
              </Button>
            ) : null}
            {isActiveRun ? (
              <Button variant="secondary" onClick={() => void handleForceRerun()} disabled={forceRerunBusy}>
                {forceRerunBusy ? "Force rerunning..." : "Force Rerun"}
              </Button>
            ) : null}
            <Button asChild variant="outline">
              <Link href={run ? `/tenants/${encodeURIComponent(run.tenant_id)}/runs` : "/tenants/select"}>
                <ArrowLeft className="mr-2 h-4 w-4" />
                Back to Runs
              </Link>
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {statusLine ? <p className="rounded-md border px-3 py-2 text-red-700">{statusLine}</p> : null}
        {run ? (
          <>
            <div className="grid gap-3 md:grid-cols-2">
              <p>
                <strong>Tenant:</strong> {run.tenant_id}
              </p>
              <p>
                <strong>Project:</strong> {run.project_id ?? "None"}
              </p>
              <p>
                <strong>Issue:</strong>{" "}
                {run.issue_url && run.issue_key ? (
                  <Link className="text-primary hover:underline" href={run.issue_url} target="_blank" rel="noopener noreferrer">
                    {run.issue_key}
                  </Link>
                ) : (
                  run.issue_key || "None"
                )}
              </p>
              <p>
                <strong>Status:</strong> {statusBadge(run.status)}
              </p>
              <p>
                <strong>Created:</strong> {new Date(run.created_at).toLocaleString()}
              </p>
              <p>
                <strong>Started:</strong> {run.started_at ? new Date(run.started_at).toLocaleString() : "Not started"}
              </p>
              <p>
                <strong>Finished:</strong> {run.finished_at ? new Date(run.finished_at).toLocaleString() : "Not finished"}
              </p>
              <p>
                <strong>Branch:</strong> {run.branch ?? "None"}
              </p>
              <p className="md:col-span-2">
                <strong>Codex session:</strong>{" "}
                {latestCodexSessionId ? (
                  <code className="rounded bg-muted px-1 py-0.5 text-xs">{latestCodexSessionId}</code>
                ) : (
                  "Not established yet"
                )}
              </p>
              <p className="md:col-span-2">
                <strong>Repository:</strong>{" "}
                {run.repo_url ? (
                  <Link className="text-primary hover:underline" href={run.repo_url} target="_blank" rel="noopener noreferrer">
                    {run.repo_url}
                  </Link>
                ) : (
                  "None"
                )}
              </p>
              <p className="md:col-span-2">
                <strong>Code Path:</strong>{" "}
                {codePath ? (
                  <>
                    <a
                      className="text-primary hover:underline"
                      href={`file://${encodeURI(codePath)}`}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      Open local path
                    </a>
                    <span className="ml-2 break-all text-muted-foreground">{codePath}</span>
                  </>
                ) : (
                  "Not available yet"
                )}
              </p>
              <p className="md:col-span-2">
                <strong>PR:</strong>{" "}
                {run.pr_url ? (
                  <Link className="text-primary hover:underline" href={run.pr_url} target="_blank">
                    {run.pr_url}
                  </Link>
                ) : (
                  "None"
                )}
              </p>
              <p className="md:col-span-2">
                <strong>Last error:</strong> {run.last_error ?? "None"}
              </p>
            </div>
            <div className="rounded-md border bg-muted/40 p-3">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Agent final outputs</p>
              <ul className="space-y-2 text-xs">
                {agentOutcomes.map((outcome) => (
                  <li
                    key={outcome.stage}
                    className="rounded border bg-background p-3"
                    style={{ borderLeft: `3px solid ${stageColor(outcome.stage)}` }}
                  >
                    <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                      {outcome.label} final output
                    </p>
                    {outcome.items.length === 0 ? (
                      <p className="text-xs text-muted-foreground">{outcome.emptyText}</p>
                    ) : (
                      <div className="space-y-1">
                        {outcome.items.map((item, idx) => (
                          <p key={`${outcome.stage}-item-${idx}`} className="whitespace-pre-wrap">
                            {item}
                          </p>
                        ))}
                      </div>
                    )}
                    {outcome.feedback ? (
                      <p className="mt-2 rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900">
                        Feedback: {outcome.feedback}
                      </p>
                    ) : null}
                  </li>
                ))}
              </ul>
              {workflowDiagnostics ? (
                <div className="mt-3 rounded border bg-background p-3">
                  <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Terminal diagnostics</p>
                  <p className="mt-1 text-xs">
                    <strong>Stage:</strong> {workflowDiagnostics.stage || "unknown"}
                  </p>
                  <p className="text-xs">
                    <strong>Message:</strong> {workflowDiagnostics.message || run?.last_error || "No diagnostics message."}
                  </p>
                </div>
              ) : null}
            </div>
            <div className="rounded-md border bg-muted/20 p-3">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Chat timeline</p>
              {chatTimelineEntries.length === 0 ? (
                <p className="text-xs text-muted-foreground">No timeline messages captured yet.</p>
              ) : (
                <ul className="max-h-[360px] space-y-2 overflow-y-auto pr-1 text-xs">
                  {chatTimelineEntries.map((entry) => (
                    <li
                      key={entry.key}
                      className="rounded border bg-background p-2"
                      style={{ borderLeft: `3px solid ${stageColor(entry.stage)}` }}
                    >
                      <p className="mb-1 text-[11px] text-muted-foreground">
                        {new Date(entry.recordedAt).toLocaleString()} · {stageDisplayLabel(entry.stage)}
                        {entry.attempt !== null ? ` #${entry.attempt}` : ""} · {entry.speaker}
                      </p>
                      <p
                        className={
                          entry.kind === "error"
                            ? "whitespace-pre-wrap text-red-700"
                            : entry.kind === "reasoning"
                              ? "whitespace-pre-wrap text-slate-700"
                              : "whitespace-pre-wrap"
                        }
                      >
                        {entry.text}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="rounded-md border bg-muted/40 p-3">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Plan JSON</p>
              <pre className="max-h-[360px] overflow-auto whitespace-pre-wrap text-xs">
                {run.plan ? JSON.stringify(run.plan, null, 2) : "No plan captured for this run."}
              </pre>
            </div>
            <div className="rounded-md border bg-muted/20 p-3">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Live stage updates</p>
              {liveStageUpdates.length === 0 ? (
                <p className="text-xs text-muted-foreground">No live stage updates captured yet.</p>
              ) : (
                <ul className="space-y-2 text-xs">
                  {liveStageUpdates.map((entry, idx) => (
                    <li key={`live-stage-${idx}`} className="rounded border p-2">
                      <p>
                        <strong>{String(entry.stage ?? "unknown_stage")}</strong>
                      </p>
                      <p className="text-muted-foreground">{String(entry.recorded_at ?? "")}</p>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="rounded-md border bg-muted/20 p-3">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Run timeline</p>
              {!runTimeline ? (
                <p className="text-xs text-muted-foreground">Timeline data will appear as soon as telemetry events are captured.</p>
              ) : (
                <div className="space-y-3">
                  <div className="grid gap-2 md:grid-cols-4">
                    <div className="rounded border bg-background p-2 text-xs">
                      <p className="text-muted-foreground">Total window</p>
                      <p className="font-semibold">{formatDuration(runTimeline.totalMs)}</p>
                    </div>
                    <div className="rounded border bg-background p-2 text-xs">
                      <p className="text-muted-foreground">Queue wait</p>
                      <p className="font-semibold">{formatDuration(runTimeline.queueWaitMs)}</p>
                    </div>
                    <div className="rounded border bg-background p-2 text-xs">
                      <p className="text-muted-foreground">Stage runtime</p>
                      <p className="font-semibold">{formatDuration(runTimeline.stageMs)}</p>
                    </div>
                    <div className="rounded border bg-background p-2 text-xs">
                      <p className="text-muted-foreground">Resumed stages</p>
                      <p className="font-semibold">{runTimeline.resumedCount}</p>
                    </div>
                  </div>
                  <div className="rounded border bg-background p-2">
                    <div className="relative h-10 overflow-hidden rounded bg-muted/50">
                      {runTimeline.segments.map((segment) => {
                        const leftPct = ((segment.startMs - runTimeline.minStartMs) / runTimeline.totalMs) * 100;
                        const widthPct = Math.max(1, (segment.durationMs / runTimeline.totalMs) * 100);
                        return (
                          <div
                            key={segment.key}
                            className="absolute top-0 h-10 text-[10px] font-semibold text-white"
                            style={{
                              left: `${leftPct}%`,
                              width: `${widthPct}%`,
                              backgroundColor: segment.color
                            }}
                            title={`${segment.label}: ${formatDuration(segment.durationMs)} (${segment.detail})`}
                          >
                            <span className="block truncate px-1 pt-3">{segment.label}</span>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                  <ul className="space-y-1 text-xs">
                    {runTimeline.segments.map((segment) => (
                      <li key={`meta-${segment.key}`} className="flex flex-wrap items-center gap-2">
                        <span
                          className="inline-block h-2 w-2 rounded-full"
                          style={{ backgroundColor: segment.color }}
                        />
                        <span className="font-semibold">{segment.label}</span>
                        <span className="text-muted-foreground">{formatDuration(segment.durationMs)}</span>
                        <span className="text-muted-foreground">{segment.detail}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
            <div className="rounded-md border bg-muted/20 p-3">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Codex session timeline
              </p>
              {invocationSessionRows.length === 0 ? (
                <p className="text-xs text-muted-foreground">No stage invocation telemetry captured yet.</p>
              ) : (
                <ul className="max-h-[280px] space-y-2 overflow-y-auto pr-1 text-xs">
                  {invocationSessionRows.map((row) => (
                    <li key={row.key} className="rounded border p-2">
                      <p>
                        <strong>{row.stage}</strong>
                        {row.attempt !== null ? ` #${row.attempt}` : ""} ·{" "}
                        <strong>
                          {row.resumedSession === null ? "unknown" : row.resumedSession ? "resumed" : "new"}
                        </strong>
                        {row.durationMs !== null ? ` · ${row.durationMs}ms` : ""}
                        {row.status ? ` · ${row.status}` : ""}
                      </p>
                      <p className="text-muted-foreground">
                        start: {row.startedAt ? new Date(row.startedAt).toLocaleString() : "n/a"} · finish:{" "}
                        {row.finishedAt ? new Date(row.finishedAt).toLocaleString() : "n/a"}
                      </p>
                      <p className="break-all text-muted-foreground">
                        session: {row.codexSessionId ?? "n/a"} · invocation: {row.invocationId}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="rounded-md border bg-muted/20 p-3">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Agent events (live)</p>
              {events.length === 0 ? (
                <p className="text-xs text-muted-foreground">No agent events captured for this run yet.</p>
              ) : (
                <ul className="max-h-[260px] space-y-2 overflow-y-auto pr-1 text-xs">
                  {events.map((event, idx) => (
                    <li key={`${event.agent_id}-${event.recorded_at}-${idx}`} className="rounded border p-2">
                      <p>
                        <strong>{event.event_type}</strong> by {event.agent_id}
                      </p>
                      <p className="text-muted-foreground">{new Date(event.recorded_at).toLocaleString()}</p>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="rounded-md border bg-muted/20 p-3">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Agent logs (live)</p>
                <div className="flex items-center gap-2 text-xs">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void handleLoadOlderLogs()}
                    disabled={loadingOlderLogs || !hasMoreLogs || logs.length === 0}
                  >
                    {loadingOlderLogs ? "Loading..." : hasMoreLogs ? "Load older logs" : "All logs loaded"}
                  </Button>
                  <label className="flex items-center gap-1">
                    Agent
                    <select
                      className="rounded border bg-background px-2 py-1"
                      value={logAgentFilter}
                      onChange={(event) => setLogAgentFilter(event.target.value)}
                    >
                      <option value="all">All</option>
                      <option value="pm">pm</option>
                      <option value="dev">dev</option>
                      <option value="tester">tester</option>
                      <option value="review">review</option>
                    </select>
                  </label>
                  <label className="flex items-center gap-1">
                    Stage
                    <select
                      className="rounded border bg-background px-2 py-1"
                      value={logStageFilter}
                      onChange={(event) => setLogStageFilter(event.target.value)}
                    >
                      <option value="all">All</option>
                      <option value="pm">pm</option>
                      <option value="dev">dev</option>
                      <option value="test">test</option>
                      <option value="review">review</option>
                    </select>
                  </label>
                  <label className="flex items-center gap-1">
                    Stream
                    <select
                      className="rounded border bg-background px-2 py-1"
                      value={logStreamFilter}
                      onChange={(event) => setLogStreamFilter(event.target.value)}
                    >
                      <option value="all">All</option>
                      <option value="stdout">stdout</option>
                      <option value="stderr">stderr</option>
                    </select>
                  </label>
                </div>
              </div>
              {logs.length === 0 ? (
                <p className="text-xs text-muted-foreground">No agent logs captured for this run yet.</p>
              ) : filteredLogs.length === 0 ? (
                <p className="text-xs text-muted-foreground">No log lines match current filters.</p>
              ) : (
                <ul className="max-h-[320px] space-y-2 overflow-y-auto pr-1 text-xs">
                  {filteredLogs.map((entry, idx) => (
                    <li key={`${entry.recorded_at}-${idx}`} className="rounded border p-2">
                      <p>
                        <strong>{entry.agent_id}</strong> · <strong>{entry.stage}</strong>
                        {entry.attempt !== null ? ` #${entry.attempt}` : ""} [{entry.stream}]
                      </p>
                      <p className="whitespace-pre-wrap">{entry.message}</p>
                      <p className="text-muted-foreground">{new Date(entry.recorded_at).toLocaleString()}</p>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </>
        ) : (
          <p className="text-muted-foreground">Loading run details...</p>
        )}
      </CardContent>
    </Card>
  );
}
