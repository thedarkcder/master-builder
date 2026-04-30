"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, usePathname, useRouter } from "next/navigation";
import { AlertCircle, ArrowLeft, Brain, ChevronRight, FileCode, MessageSquare, Terminal, Wrench } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useToast } from "@/components/ui/toast-provider";
import { TokenStackedBarChart } from "@/components/charts";
import {
  cancelRun,
  createWorkflowAttempt,
  getRun,
  getWorkflow,
  getTokenTimeline,
  listRunEvents,
  listRunLogs,
  type RunEventRecord,
  type RuntimeLogEventRecord,
  type RunRecord,
  type WorkflowRecord,
  type WorkflowAttemptCreatePayload,
  type TokenTimelineRecord
} from "@/lib/api";
import { formatTimeAgo, formatTimestamp } from "@/lib/datetime";
import { buildRunDetailPath, resolveRunRouteContext } from "@/lib/dashboard-paths";
import { useRunEventStream } from "@/hooks/use-run-event-stream";

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

type StageCheckpointEntry = {
  status: string;
  completedAt: string | null;
  summary: string;
};

type StageProgressStatus = "not_started" | "running" | "completed" | "interrupted";

type StageProgressEntry = {
  status: StageProgressStatus;
  tileDetail: string;
  detail: string;
  sortKey: number;
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

type OrchestrationStageTraceEntry = {
  order: number;
  stage: string;
  status: string;
  summary: string;
  startedAt: string | null;
  finishedAt: string | null;
  invocationId: string | null;
  attempt: number | null;
  durationMs: number | null;
};

type OrchestrationWorkstreamTraceEntry = {
  order: number;
  name: string;
  stage: string;
  status: string;
  summary: string;
  branch: string | null;
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
  kind: "message" | "reasoning" | "status" | "error" | "command" | "file_edit" | "tool_call";
  meta?: {
    command?: string;
    exitCode?: number;
    output?: string;
    filePath?: string;
    language?: string;
  };
};
type RunPanelTab = "overview" | "agents" | "diagnostics" | "cost";
type AgentStage = "pm" | "dev" | "test" | "review";
const CHAT_PAGE_SIZE = 40;

type RerunAttemptOption = {
  key: string;
  label: string;
  payload: WorkflowAttemptCreatePayload;
  detail: string;
};

const PANEL_TABS: { id: RunPanelTab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "agents", label: "Agents" },
  { id: "diagnostics", label: "Diagnostics" },
  { id: "cost", label: "Cost" }
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function toStringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => String(item ?? "").trim()).filter((item) => item.length > 0);
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

function parseStageCheckpoints(plan: Record<string, unknown> | null | undefined): Partial<Record<AgentStage, StageCheckpointEntry>> {
  if (!isRecord(plan)) {
    return {};
  }
  const stagesRaw = isRecord(plan["stages"]) ? plan["stages"] : null;
  if (!stagesRaw) {
    return {};
  }
  const parsed: Partial<Record<AgentStage, StageCheckpointEntry>> = {};
  for (const stage of ["pm", "dev", "test", "review"] as AgentStage[]) {
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

function parseExecutionContext(plan: Record<string, unknown> | null | undefined): Record<string, string> {
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

function parseStageArtifact(
  plan: Record<string, unknown> | null | undefined,
  stage: AgentStage,
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

function formatTokenCount(value: number): string {
  const normalized = Math.max(0, Math.floor(value));
  if (normalized >= 1000_000) {
    return `${(normalized / 1000_000).toFixed(2)}m`;
  }
  if (normalized >= 1000) {
    return `${(normalized / 1000).toFixed(1)}k`;
  }
  return String(normalized);
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
    case "orchestrated_run":
      return "#8b5cf6";
    default:
      return "#64748b";
  }
}

function stageDisplayLabel(stage: string): string {
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

function logEntryIdentity(entry: RuntimeLogEventRecord): string {
  if (entry.event_id) {
    return entry.event_id;
  }
  return [
    entry.recorded_at,
    entry.stage,
    entry.stream,
    String(entry.invocation_id ?? "").trim(),
    String(entry.command ?? "").trim(),
    entry.message
  ].join("::");
}

function dedupeRunLogs(entries: RuntimeLogEventRecord[]): RuntimeLogEventRecord[] {
  const seen = new Set<string>();
  const ordered: RuntimeLogEventRecord[] = [];
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

type ParsedChatEntry = Pick<ChatTimelineEntry, "speaker" | "text" | "kind"> & { meta?: ChatTimelineEntry["meta"] };

function tryParseToolRequest(text: string): ParsedChatEntry | null {
  if (!text.startsWith("{")) return null;
  let inner: unknown = null;
  try { inner = JSON.parse(text); } catch { return null; }
  if (!isRecord(inner)) return null;
  const t = String(inner.type ?? "").trim().toLowerCase();
  if (t === "tool_request" || t === "tool_call" || t === "function_call") {
    const toolName = String(inner.tool_name ?? inner.name ?? inner.function_name ?? "tool").trim();
    const reason = String(inner.reason ?? "").trim();
    return {
      speaker: "codex",
      text: toolName,
      kind: "tool_call",
      meta: { output: reason || undefined }
    };
  }
  return null;
}

function parseRunLogChatText(entry: RuntimeLogEventRecord): ParsedChatEntry | null {
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

  if (eventType === "tool_request" || eventType === "tool_call" || eventType === "function_call") {
    const toolName = String(parsed.tool_name ?? parsed.name ?? parsed.function_name ?? "tool").trim();
    const reason = String(parsed.reason ?? "").trim();
    return {
      speaker: "codex",
      text: toolName,
      kind: "tool_call",
      meta: { output: reason || undefined }
    };
  }

  const item = isRecord(parsed.item) ? parsed.item : null;
  if (eventType === "item.completed" && item) {
    const itemType = String(item.type ?? "").trim().toLowerCase();

    if (itemType === "agent_message") {
      const msgText = String(item.text ?? "").trim();
      const innerObj = tryParseToolRequest(msgText);
      if (innerObj) return innerObj;
      return { speaker: "codex", text: msgText, kind: "message" };
    }

    if (itemType === "reasoning") {
      return { speaker: "codex", text: String(item.text ?? "").trim(), kind: "reasoning" };
    }

    if (itemType === "command_execution") {
      const status = String(item.status ?? "").trim().toLowerCase();
      const exitCode = typeof item.exit_code === "number" ? item.exit_code : null;
      const command = normalizeInlineText(String(item.command ?? ""));
      const output = String(item.aggregated_output ?? "").trim();
      const isFailed = status === "failed" || (exitCode !== null && exitCode !== 0);

      if (isFailed) {
        return {
          speaker: "terminal",
          text: `Command failed${exitCode !== null ? ` (exit ${exitCode})` : ""}: ${command}`,
          kind: "error",
          meta: { command, exitCode: exitCode ?? undefined, output: output || undefined }
        };
      }
      return {
        speaker: "terminal",
        text: command,
        kind: "command",
        meta: { command, exitCode: exitCode ?? 0, output: output || undefined }
      };
    }

    if (itemType === "file_edit" || itemType === "file_create" || itemType === "file_write") {
      const filePath = String(item.file_path ?? item.path ?? "").trim();
      const language = filePath.split(".").pop() ?? "";
      const content = String(item.content ?? item.diff ?? item.text ?? "").trim();
      return {
        speaker: "codex",
        text: `${itemType === "file_create" || itemType === "file_write" ? "Created" : "Edited"} ${filePath || "file"}`,
        kind: "file_edit",
        meta: { filePath, language, output: content || undefined }
      };
    }

    if (itemType === "tool_call" || itemType === "function_call" || itemType === "mcp_call") {
      const toolName = String(item.name ?? item.tool_name ?? item.function_name ?? "tool").trim();
      const result = String(item.output ?? item.result ?? "").trim();
      return {
        speaker: "codex",
        text: toolName,
        kind: "tool_call",
        meta: { output: result || undefined }
      };
    }

    if (itemType) {
      const fallbackText = String(item.text ?? "").trim();
      const innerObj = tryParseToolRequest(fallbackText);
      if (innerObj) return innerObj;
      return {
        speaker: "codex",
        text: `${itemType}${fallbackText ? `: ${normalizeInlineText(fallbackText).slice(0, 200)}` : ""}`,
        kind: "status"
      };
    }
  }

  return null;
}

const MAX_OUTPUT_PREVIEW = 600;

const TIMELINE_ICON: Record<ChatTimelineEntry["kind"], { icon: React.ReactNode; color: string }> = {
  message:   { icon: <MessageSquare className="h-3.5 w-3.5" />, color: "text-blue-500" },
  reasoning: { icon: <Brain className="h-3.5 w-3.5" />,          color: "text-violet-500" },
  status:    { icon: <ChevronRight className="h-3.5 w-3.5" />,   color: "text-muted-foreground" },
  error:     { icon: <AlertCircle className="h-3.5 w-3.5" />,    color: "text-red-500" },
  command:   { icon: <Terminal className="h-3.5 w-3.5" />,       color: "text-amber-500" },
  file_edit: { icon: <FileCode className="h-3.5 w-3.5" />,      color: "text-emerald-500" },
  tool_call: { icon: <Wrench className="h-3.5 w-3.5" />,        color: "text-orange-500" },
};

function TimelineRow({
  entry,
  stageDisplayLabelFn,
  isLast,
}: {
  entry: ChatTimelineEntry;
  stageDisplayLabelFn: (stage: string) => string;
  isLast: boolean;
}) {
  const { icon, color } = TIMELINE_ICON[entry.kind] ?? TIMELINE_ICON.message;
  const stageLbl = stageDisplayLabelFn(entry.stage);
  const ts = formatTimeAgo(entry.recordedAt);
  const fullTs = formatTimestamp(entry.recordedAt);
  const attempt = entry.attempt !== null ? ` #${entry.attempt}` : "";

  return (
    <li className="group relative flex gap-3 pb-4 last:pb-0">
      {/* vertical connector line */}
      {!isLast && (
        <div className="absolute left-[13px] top-6 bottom-0 w-px bg-border" />
      )}

      {/* dot / icon */}
      <div className={`relative z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border bg-background ${color}`}>
        {icon}
      </div>

      {/* content */}
      <div className="min-w-0 flex-1 pt-0.5">
        {/* header */}
        <div className="flex items-baseline gap-1.5 text-xs">
          <span className="font-medium text-foreground">
            {entry.kind === "status" ? entry.text : entry.kind === "command" ? "Ran command" : entry.kind === "file_edit" ? "Edited file" : entry.kind === "tool_call" ? `Called ${entry.text}` : entry.kind === "reasoning" ? "Thinking" : entry.kind === "error" ? "Error" : stageLbl}
          </span>
          <span className="text-[10px] text-muted-foreground">{stageLbl}{attempt}</span>
          <span className="ml-auto shrink-0 text-[10px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" title={fullTs}>{ts}</span>
        </div>

        {/* body per kind */}
        {entry.kind === "status" ? null : entry.kind === "reasoning" ? (
          <details className="mt-1 text-xs text-muted-foreground">
            <summary className="cursor-pointer select-none hover:text-foreground">Show reasoning</summary>
            <p className="mt-1.5 whitespace-pre-wrap italic leading-relaxed">{entry.text}</p>
          </details>
        ) : entry.kind === "command" ? (
          <div className="mt-1">
            <div className="inline-flex items-center gap-1.5 rounded-md bg-zinc-950 px-2.5 py-1 text-[11px] text-zinc-200">
              <code>{entry.meta?.command ?? entry.text}</code>
              {entry.meta?.exitCode != null && (
                <span className={`ml-1 rounded px-1 py-px text-[9px] font-medium ${entry.meta.exitCode === 0 ? "bg-emerald-900/50 text-emerald-300" : "bg-red-900/50 text-red-300"}`}>
                  {entry.meta.exitCode}
                </span>
              )}
            </div>
            {entry.meta?.output ? (
              <details className="mt-1.5 text-xs text-muted-foreground">
                <summary className="cursor-pointer select-none hover:text-foreground">Output</summary>
                <pre className="mt-1 max-h-40 overflow-auto rounded-md bg-muted/50 px-2.5 py-2 text-[10px] leading-relaxed">
                  {entry.meta.output.slice(0, MAX_OUTPUT_PREVIEW)}{entry.meta.output.length > MAX_OUTPUT_PREVIEW ? "\n…" : ""}
                </pre>
              </details>
            ) : null}
          </div>
        ) : entry.kind === "file_edit" ? (
          <div className="mt-1">
            <code className="text-xs font-medium">{entry.meta?.filePath || "file"}</code>
            {entry.text && <span className="ml-1.5 text-xs text-muted-foreground">{entry.text}</span>}
            {entry.meta?.output ? (
              <details className="mt-1.5 text-xs text-muted-foreground">
                <summary className="cursor-pointer select-none hover:text-foreground">Diff</summary>
                <pre className="mt-1 max-h-40 overflow-auto rounded-md bg-muted/50 px-2.5 py-2 text-[10px] leading-relaxed">
                  {entry.meta.output.slice(0, MAX_OUTPUT_PREVIEW)}{entry.meta.output.length > MAX_OUTPUT_PREVIEW ? "\n…" : ""}
                </pre>
              </details>
            ) : null}
          </div>
        ) : entry.kind === "tool_call" ? (
          <div className="mt-1 text-xs">
            {entry.meta?.output ? (
              <span className="text-muted-foreground">{entry.meta.output.slice(0, 160)}{entry.meta.output.length > 160 ? "…" : ""}</span>
            ) : null}
          </div>
        ) : entry.kind === "error" ? (
          <p className="mt-1 whitespace-pre-wrap text-xs leading-relaxed text-destructive">{entry.text}</p>
        ) : (
          <p className="mt-1 whitespace-pre-wrap text-xs leading-relaxed">{entry.text}</p>
        )}
      </div>
    </li>
  );
}

export default function RunDetailPage() {
  const params = useParams<{ runId: string }>();
  const pathname = usePathname();
  const router = useRouter();
  const { credentials, ready } = useAuth();
  const { showToast } = useToast();
  const routeContext = useMemo(() => resolveRunRouteContext(pathname), [pathname]);
  const [run, setRun] = useState<RunRecord | null>(null);
  const [workflow, setWorkflow] = useState<WorkflowRecord | null>(null);
  const [events, setEvents] = useState<RunEventRecord[]>([]);
  const [logs, setLogs] = useState<RuntimeLogEventRecord[]>([]);
  const [busy, setBusy] = useState(false);
  const [rerunBusy, setRerunBusy] = useState(false);
  const [forceRerunBusy, setForceRerunBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [tokenTimeline, setTokenTimeline] = useState<TokenTimelineRecord | null>(null);
  const [tokenTimelineBusy, setTokenTimelineBusy] = useState(false);
  const [tokenTimelineError, setTokenTimelineError] = useState("");
  const [logAgentFilter, setLogAgentFilter] = useState("all");
  const [logStageFilter, setLogStageFilter] = useState("all");
  const [logStreamFilter, setLogStreamFilter] = useState("all");
  const [loadingOlderLogs, setLoadingOlderLogs] = useState(false);
  const [hasMoreLogs, setHasMoreLogs] = useState(false);
  const projectContextId = routeContext.projectId ?? "";
  const activePanel = routeContext.panel;
  const setActivePanel = useCallback((nextPanel: RunPanelTab) => {
    router.push(
      buildRunDetailPath({
        tenantId: routeContext.tenantId ?? run?.tenant_id ?? null,
        projectId: routeContext.projectId ?? null,
        runId: params.runId,
        panel: nextPanel,
      })
    );
  }, [params.runId, routeContext.projectId, routeContext.tenantId, router, run?.tenant_id]);
  const [chatVisibleCount, setChatVisibleCount] = useState(CHAT_PAGE_SIZE);
  const [chatAutoScroll, setChatAutoScroll] = useState(true);
  const chatListRef = useRef<HTMLUListElement | null>(null);

  const loadRun = useCallback(async () => {
    if (!credentials) {
      return;
    }
    setBusy(true);
    setTokenTimelineBusy(true);
    setTokenTimelineError("");
    try {
      const [runPayload, runEvents, runLogs] = await Promise.all([
        getRun(credentials, params.runId),
        listRunEvents(credentials, params.runId, { limit: 200 }),
        listRunLogs(credentials, params.runId, { limit: 200 })
      ]);
      let timeline: TokenTimelineRecord | null = null;
      let workflowPayload: WorkflowRecord | null = null;
      const [timelineResult, workflowResult] = await Promise.allSettled([
        getTokenTimeline(credentials, params.runId, {
          tenantId: runPayload.tenant_id,
          include_retries: true
        }),
        getWorkflow(credentials, runPayload.workflow_id),
      ]);
      if (timelineResult.status === "fulfilled") {
        timeline = timelineResult.value;
      } else {
        setTokenTimelineError(`Failed to load token timeline: ${(timelineResult.reason as Error).message}`);
      }
      if (workflowResult.status === "fulfilled") {
        workflowPayload = workflowResult.value;
      }
      setRun(runPayload);
      setWorkflow(workflowPayload);
      setEvents(runEvents);
      setLogs(dedupeRunLogs(runLogs));
      setTokenTimeline(timeline);
      setHasMoreLogs(runLogs.length >= 200);
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to load run: ${(error as Error).message}`);
      setTokenTimelineError(`Failed to load token timeline: ${(error as Error).message}`);
      setWorkflow(null);
    } finally {
      setBusy(false);
      setTokenTimelineBusy(false);
    }
  }, [credentials, params.runId]);

  useEffect(() => {
    if (ready && credentials) {
      void loadRun();
    }
  }, [ready, credentials, loadRun]);

  const runStatus = run?.status ?? null;
  const runId = run?.run_id ?? null;

  const handleStreamLogEvent = useCallback((logEvent: RuntimeLogEventRecord) => {
    setLogs((prev) => {
      const nextKey = logEntryIdentity(logEvent);
      if (prev.some((entry) => logEntryIdentity(entry) === nextKey)) {
        return prev;
      }
      const next = [...prev, logEvent];
      return dedupeRunLogs(next).slice(-800);
    });
  }, []);

  const handleStreamLifecycleEvent = useCallback((lifecycleEvent: RunEventRecord) => {
    setEvents((prev) => {
      if (
        prev.some(
          (entry) =>
            entry.event_type === lifecycleEvent.event_type &&
            entry.recorded_at === lifecycleEvent.recorded_at &&
            entry.agent_id === lifecycleEvent.agent_id,
        )
      ) {
        return prev;
      }
      const next = [...prev, lifecycleEvent];
      return next.slice(-200);
    });
  }, []);

  const handleStreamStatusChange = useCallback((nextStatus: RunRecord["status"]) => {
    setRun((prev) => (prev && prev.status !== nextStatus ? { ...prev, status: nextStatus } : prev));
  }, []);

  const handleStreamError = useCallback((message: string) => {
    setStatusLine(`Run event stream closed: ${message}`);
  }, []);

  useRunEventStream({
    credentials,
    runId,
    runStatus,
    onLogEvent: handleStreamLogEvent,
    onLifecycleEvent: handleStreamLifecycleEvent,
    onStatusChange: handleStreamStatusChange,
    onError: handleStreamError,
  });

  async function handleForceRerun() {
    if (!credentials || !run) {
      return;
    }
    setForceRerunBusy(true);
    try {
      const cancelled = await cancelRun(credentials, run.run_id);
      const nextRun = await createWorkflowAttempt(credentials, cancelled.workflow_id, {
        mode: "fresh"
      });
      showToast({ title: "Fresh run queued", description: `Cancelled ${cancelled.run_id}; queued ${nextRun.run_id}.`, tone: "success" });
      router.push(
        buildRunDetailPath({
          tenantId: run.tenant_id,
          projectId: projectContextId || null,
          runId: nextRun.run_id,
        })
      );
    } catch (error) {
      showToast({ title: "Force rerun failed", description: (error as Error).message, tone: "error" });
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
        beforeRecordedAt: oldest.recorded_at,
        beforeEventId: oldest.event_id
      });
      setLogs((prev) => {
        return dedupeRunLogs([...prev, ...olderLogs]);
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
  const liveStageUpdates = useMemo(() => {
    if (!isRecord(run?.plan)) {
      return [] as Array<Record<string, unknown>>;
    }
    const eventsRoot = isRecord(run.plan["events"]) ? run.plan["events"] : null;
    return Array.isArray(eventsRoot?.["live_stage_updates"])
      ? (eventsRoot["live_stage_updates"] as Array<Record<string, unknown>>)
      : [];
  }, [run?.plan]);
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
  const tokenTimelineChartData = useMemo(
    () =>
      (tokenTimeline?.turns ?? []).map((turn, index) => ({
        turnOrder: index + 1,
        turnLabel: String(index + 1),
        input_tokens: turn.input_tokens,
        cached_input_tokens: turn.cached_input_tokens,
        output_tokens: turn.output_tokens,
        uncached_input_tokens: Math.max(0, turn.input_tokens - turn.cached_input_tokens),
        delta_input: turn.delta_input,
        delta_uncached: turn.delta_uncached,
        delta_output: turn.delta_output,
        stage: turn.stage,
      })),
    [tokenTimeline?.turns]
  );
  const tokenTurnRows = useMemo(
    () => {
      const keyCounts = new Map<string, number>();
      return (tokenTimeline?.turns ?? []).map((turn, index) => {
        const turnId = String(turn.turn_id ?? "").trim();
        const fallbackKey = [
          String(turn.invocation_id ?? "").trim(),
          String(turn.stage ?? "").trim(),
          turn.attempt === null ? "na" : String(turn.attempt),
          String(turn.recorded_at ?? "").trim(),
        ]
          .filter((part) => part.length > 0)
          .join("::");
        const baseKey = turnId || fallbackKey || `token-turn-${index + 1}`;
        const seenCount = keyCounts.get(baseKey) ?? 0;
        keyCounts.set(baseKey, seenCount + 1);
        const rowKey = seenCount === 0 ? baseKey : `${baseKey}::${seenCount + 1}`;
        return {
          ...turn,
          rowKey,
          turnOrder: index + 1,
          uncached_input_tokens: Math.max(0, turn.input_tokens - turn.cached_input_tokens),
        };
      });
    },
    [tokenTimeline?.turns]
  );
  const orchestrationTrace = useMemo(() => {
    if (!isRecord(run?.plan)) {
      return {
        stageEvents: [] as OrchestrationStageTraceEntry[],
        workstreamEvents: [] as OrchestrationWorkstreamTraceEntry[],
      };
    }
    const eventsRoot = isRecord(run.plan["events"]) ? run.plan["events"] : null;
    const stageRaw = Array.isArray(eventsRoot?.["stage_trace"])
      ? (eventsRoot["stage_trace"] as unknown[])
      : [];
    const workstreamRaw = Array.isArray(eventsRoot?.["workstream_trace"])
      ? (eventsRoot["workstream_trace"] as unknown[])
      : [];
    const stageEvents: OrchestrationStageTraceEntry[] = stageRaw
      .map((item, index) => {
        if (!isRecord(item)) {
          return null;
        }
        const stage = String(item["stage"] ?? "").trim().toLowerCase();
        if (!stage) {
          return null;
        }
        return {
          order: Number.isFinite(Number(item["order"])) ? Number(item["order"]) : index,
          stage,
          status: String(item["status"] ?? "").trim().toLowerCase() || "completed",
          summary: String(item["summary"] ?? "").trim(),
          startedAt: String(item["started_at"] ?? "").trim() || null,
          finishedAt: String(item["finished_at"] ?? "").trim() || null,
          invocationId: String(item["invocation_id"] ?? "").trim() || null,
          attempt: Number.isFinite(Number(item["attempt"])) ? Number(item["attempt"]) : null,
          durationMs: Number.isFinite(Number(item["duration_ms"])) ? Number(item["duration_ms"]) : null,
        } satisfies OrchestrationStageTraceEntry;
      })
      .filter((item): item is OrchestrationStageTraceEntry => item !== null)
      .sort((a, b) => a.order - b.order);
    const workstreamEvents: OrchestrationWorkstreamTraceEntry[] = workstreamRaw
      .map((item, index) => {
        if (!isRecord(item)) {
          return null;
        }
        const name = String(item["name"] ?? "").trim();
        if (!name) {
          return null;
        }
        return {
          order: Number.isFinite(Number(item["order"])) ? Number(item["order"]) : index,
          name,
          stage: String(item["stage"] ?? "").trim().toLowerCase() || "dev",
          status: String(item["status"] ?? "").trim().toLowerCase() || "completed",
          summary: String(item["summary"] ?? "").trim(),
          branch: String(item["branch"] ?? "").trim() || null,
        } satisfies OrchestrationWorkstreamTraceEntry;
      })
      .filter((item): item is OrchestrationWorkstreamTraceEntry => item !== null)
      .sort((a, b) => a.order - b.order);
    return { stageEvents, workstreamEvents };
  }, [run?.plan]);
  const stageCheckpoints = useMemo(() => parseStageCheckpoints(run?.plan ?? null), [run?.plan]);
  const executionContext = useMemo(() => parseExecutionContext(run?.plan ?? null), [run?.plan]);
  const workflowCheckpointAvailability = useMemo(() => {
    const availability = { pm: false, execution: false };
    if (!workflow) {
      return availability;
    }
    for (const workflowRun of workflow.runs ?? []) {
      const checkpoints = parseStageCheckpoints(workflowRun.plan ?? null);
      if (checkpoints.pm) {
        availability.pm = true;
      }
      if (checkpoints.dev || checkpoints.test || checkpoints.review) {
        availability.execution = true;
      }
      if (availability.pm && availability.execution) {
        break;
      }
    }
    return availability;
  }, [workflow]);
  const hasExecutionCheckpoint = Boolean(
    stageCheckpoints.dev ||
    stageCheckpoints.test ||
    stageCheckpoints.review ||
    workflowCheckpointAvailability.execution,
  );
  const hasPmCheckpoint = Boolean(stageCheckpoints.pm || workflowCheckpointAvailability.pm);
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
    const syntheticBaseTimestamp = run?.started_at ?? run?.created_at ?? new Date().toISOString();
    for (const [index, item] of orchestrationTrace.stageEvents.entries()) {
      if (!["pm", "dev", "test", "review"].includes(item.stage)) {
        continue;
      }
      const invocationId = item.invocationId ?? `orchestration-trace-${item.stage}-${item.order}`;
      if (byInvocation.has(invocationId)) {
        continue;
      }
      byInvocation.set(invocationId, {
        key: invocationId,
        stage: item.stage,
        attempt: item.attempt ?? 1,
        invocationId,
        startedAt: item.startedAt ?? syntheticBaseTimestamp,
        finishedAt:
          item.finishedAt ??
          (item.status === "running" ? null : item.startedAt ?? syntheticBaseTimestamp),
        status: item.status,
        durationMs: item.durationMs,
        resumedSession: false,
        codexSessionId: null,
      });
      if (index > 24) {
        break;
      }
    }
    return Array.from(byInvocation.values()).sort((a, b) => {
      const aTime = new Date(a.startedAt ?? a.finishedAt ?? 0).getTime();
      const bTime = new Date(b.startedAt ?? b.finishedAt ?? 0).getTime();
      return bTime - aTime;
    });
  }, [logs, orchestrationTrace.stageEvents, run?.created_at, run?.started_at]);
  const latestCodexSessionId = useMemo(() => {
    const fromTimeline = invocationSessionRows.find((row) => row.codexSessionId)?.codexSessionId;
    return fromTimeline ?? null;
  }, [invocationSessionRows]);
  const rerunOptions = useMemo(() => {
    if (!run) {
      return [] as RerunAttemptOption[];
    }
    const options: RerunAttemptOption[] = [];
    options.push({
      key: "fresh",
      label: "Start from the start",
      payload: { mode: "fresh" },
      detail: "Create a brand-new run with no checkpoint or prior thread reuse."
    });
    if (hasExecutionCheckpoint) {
      options.push({
        key: "execution",
        label: "Resume execution",
        payload: { mode: "resume", checkpoint_kind: "execution" },
        detail: "Continue from the latest dev/test/review checkpoint."
      });
    }
    if (hasPmCheckpoint) {
      options.push({
        key: "pm",
        label: "Resume PM",
        payload: { mode: "resume", checkpoint_kind: "pm" },
        detail: "Continue from the latest PM checkpoint."
      });
      options.push({
        key: "restart-pm",
        label: "Restart from PM",
        payload: { mode: "restart", checkpoint_kind: "pm" },
        detail: "Create a new attempt from the PM checkpoint."
      });
    } else if (hasExecutionCheckpoint) {
      options.push({
        key: "restart-execution",
        label: "Restart from execution",
        payload: { mode: "restart", checkpoint_kind: "execution" },
        detail: "Create a new attempt from the latest execution checkpoint."
      });
    }
    return options;
  }, [run, hasExecutionCheckpoint, hasPmCheckpoint]);

  async function handleRerunSelection(payload: WorkflowAttemptCreatePayload, label: string) {
    if (!credentials || !run) {
      return;
    }
    setRerunBusy(true);
    try {
      const nextRun = await createWorkflowAttempt(credentials, run.workflow_id, payload);
      showToast({ title: `${label} queued`, description: `Run ${nextRun.run_id} for ${nextRun.issue_key}.`, tone: "success" });
      router.push(
        buildRunDetailPath({
          tenantId: run.tenant_id,
          projectId: projectContextId || null,
          runId: nextRun.run_id,
        })
      );
    } catch (error) {
      showToast({ title: "Rerun failed", description: (error as Error).message, tone: "error" });
    } finally {
      setRerunBusy(false);
    }
  }
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
  const terminalFailureMessage = useMemo(() => {
    // `run.last_error` is the authoritative terminal error persisted by the worker.
    const fromRun = String(run?.last_error ?? "").trim();
    if (fromRun) {
      return fromRun;
    }
    const fromDiagnostics = String(workflowDiagnostics?.message ?? "").trim();
    if (fromDiagnostics) {
      return fromDiagnostics;
    }
    return "";
  }, [run?.last_error, workflowDiagnostics?.message]);
  const secondaryFailureDetail = useMemo(() => {
    const fromRun = String(run?.last_error ?? "").trim();
    const fromDiagnostics = String(workflowDiagnostics?.message ?? "").trim();
    if (!fromRun || !fromDiagnostics) {
      return "";
    }
    if (fromRun === fromDiagnostics) {
      return "";
    }
    return fromDiagnostics;
  }, [run?.last_error, workflowDiagnostics?.message]);
  const terminalFailureHighlights = useMemo(() => {
    const items = (workflowDiagnostics?.history ?? [])
      .filter((entry) => entry.stage.toLowerCase() === "review")
      .map((entry) => entry.event.trim())
      .filter((entry) => entry.length > 0);
    return items.slice(0, 6);
  }, [workflowDiagnostics?.history]);
  const agentOutcomes = useMemo(() => {
    const pmArtifact = parseStageArtifact(run?.plan ?? null, "pm");
    const devArtifact = parseStageArtifact(run?.plan ?? null, "dev");
    const testArtifact = parseStageArtifact(run?.plan ?? null, "test");
    const reviewArtifact = parseStageArtifact(run?.plan ?? null, "review");
    const checkpointSummaryForStage = (stage: AgentStage): string[] => {
      const checkpoint = stageCheckpoints[stage];
      if (!checkpoint || !checkpoint.summary) {
        return [];
      }
      return [checkpoint.summary];
    };
    const pmItems = [
      ...checkpointSummaryForStage("pm"),
      ...toStringList(pmArtifact?.["plan_steps"]),
      ...toStringList(pmArtifact?.["acceptance_criteria"]).map((item) => `AC: ${item}`),
      ...toStringList(pmArtifact?.["risks"]).map((item) => `Risk: ${item}`)
    ];
    const nextStage = pmArtifact ? String(pmArtifact["next_stage"] ?? "").trim() : "";
    const executionWorker = pmArtifact ? String(pmArtifact["execution_worker_capability"] ?? "").trim() : "";
    if (nextStage) {
      pmItems.push(`Next stage: ${nextStage}`);
    }
    if (executionWorker) {
      pmItems.push(`Execution worker: ${executionWorker}`);
    }
    const reviewHistory = (workflowDiagnostics?.history ?? [])
      .filter((entry) => entry.stage.toLowerCase() === "review")
      .map((entry) => entry.event);
    const workstreamSummaries = orchestrationTrace.workstreamEvents.map((entry) => {
      const detail = `${entry.name} · ${entry.stage} · ${entry.status}`;
      if (entry.summary) {
        return `${detail}: ${entry.summary}`;
      }
      return detail;
    });
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
        items: [...checkpointSummaryForStage("dev"), ...toStringList(devArtifact?.["change_summary"]), ...workstreamSummaries],
        feedback: null as string | null,
        emptyText: "No Dev rationale captured."
      },
      {
        stage: "test",
        label: "Test",
        items: [...checkpointSummaryForStage("test"), ...toStringList(testArtifact?.["guidance"])],
        feedback: null as string | null,
        emptyText: "No test guidance captured."
      },
      {
        stage: "review",
        label: "Review",
        items: [...checkpointSummaryForStage("review"), ...toStringList(reviewArtifact?.["summary"]), ...reviewHistory],
        feedback: String(reviewArtifact?.["feedback"] ?? "").trim() || null,
        emptyText: "No review summary captured."
      }
    ];
  }, [orchestrationTrace.workstreamEvents, run?.plan, stageCheckpoints, workflowDiagnostics?.history]);
  const stageLiveSnapshots = useMemo(() => {
    const snapshots = new Map<AgentStage, { recordedAt: string; text: string }>();
    const validStages = new Set<AgentStage>(["pm", "dev", "test", "review"]);
    const ordered = logs
      .slice()
      .sort((a, b) => new Date(a.recorded_at).getTime() - new Date(b.recorded_at).getTime());
    for (const entry of ordered) {
      const stage = String(entry.stage ?? "").trim().toLowerCase() as AgentStage;
      if (!validStages.has(stage)) {
        continue;
      }
      const parsed = parseRunLogChatText(entry);
      if (!parsed) {
        continue;
      }
      if (parsed.kind !== "message" && parsed.kind !== "reasoning" && parsed.kind !== "error") {
        continue;
      }
      if (!parsed.text.trim()) {
        continue;
      }
      snapshots.set(stage, { recordedAt: entry.recorded_at, text: parsed.text.trim() });
    }
    return snapshots;
  }, [logs]);
  const stageProgress = useMemo(() => {
    const stages: Record<AgentStage, StageProgressEntry> = {
      pm: { status: "not_started", tileDetail: "not started", detail: "not started", sortKey: 0 },
      dev: { status: "not_started", tileDetail: "not started", detail: "not started", sortKey: 0 },
      test: { status: "not_started", tileDetail: "not started", detail: "not started", sortKey: 0 },
      review: { status: "not_started", tileDetail: "not started", detail: "not started", sortKey: 0 }
    };
    const timeOf = (value: string | null): number => (value ? new Date(value).getTime() : 0);
    for (const stage of ["pm", "dev", "test", "review"] as AgentStage[]) {
      const checkpoint = stageCheckpoints[stage];
      if (!checkpoint) {
        continue;
      }
      const completedLabel = checkpoint.completedAt
        ? `completed · ${new Date(checkpoint.completedAt).toLocaleTimeString()}`
        : "completed";
      const checkpointStatus = checkpoint.status === "completed" ? "completed" : "interrupted";
      stages[stage] = {
        status: checkpointStatus,
        tileDetail: checkpointStatus === "completed" ? "completed" : "interrupted",
        detail: checkpoint.summary ? `${completedLabel} · ${checkpoint.summary}` : completedLabel,
        sortKey: timeOf(checkpoint.completedAt),
      };
    }
    for (const row of invocationSessionRows) {
      const stage = String(row.stage ?? "").trim().toLowerCase() as AgentStage;
      if (!(stage in stages)) {
        continue;
      }
      if (stageCheckpoints[stage]?.status === "completed") {
        if (row.finishedAt && row.durationMs !== null) {
          stages[stage] = {
            ...stages[stage],
            tileDetail: `${formatDuration(row.durationMs)}`,
            sortKey: Math.max(stages[stage].sortKey, timeOf(row.finishedAt)),
          };
        }
        continue;
      }
      const current = stages[stage];
      const rowRank = Math.max(timeOf(row.finishedAt), timeOf(row.startedAt));
      if (rowRank < current.sortKey) {
        continue;
      }
      if (row.finishedAt) {
        if (isActiveRun) {
          const duration = row.durationMs !== null ? `${formatDuration(row.durationMs)}` : "completed";
          stages[stage] = {
            status: "completed",
            tileDetail: duration,
            detail: `completed · ${duration}`,
            sortKey: rowRank,
          };
          continue;
        }
        stages[stage] = {
          status: "interrupted",
          tileDetail: "interrupted",
          detail: "agent finished, checkpoint missing",
          sortKey: rowRank,
        };
      } else if (row.startedAt) {
        if (!isActiveRun) {
          stages[stage] = {
            status: "interrupted",
            tileDetail: "interrupted",
            detail: "interrupted before completion",
            sortKey: rowRank,
          };
          continue;
        }
        const startMs = new Date(row.startedAt).getTime();
        const runningMs = Number.isFinite(startMs) ? Math.max(0, Date.now() - startMs) : 0;
        const runtimeLabel = `${formatDuration(runningMs)}`;
        stages[stage] = {
          status: "running",
          tileDetail: runtimeLabel,
          detail: `running · ${runtimeLabel}`,
          sortKey: rowRank,
        };
      }
    }
    return stages;
  }, [invocationSessionRows, isActiveRun, stageCheckpoints]);
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
      if (!row.startedAt) {
        continue;
      }
      const startMs = new Date(row.startedAt).getTime();
      const endMs = row.finishedAt ? new Date(row.finishedAt).getTime() : (isActiveRun ? nowMs : NaN);
      if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) {
        continue;
      }
      const inProgress = !row.finishedAt;
      segments.push({
        key: `stage-${row.invocationId}`,
        label: row.stage.toUpperCase(),
        stage: row.stage,
        startMs,
        endMs,
        durationMs: endMs - startMs,
        color: stageColor(row.stage),
        detail: `${inProgress ? "in progress" : row.resumedSession ? "resumed session" : "new session"}${row.attempt !== null ? ` · attempt ${row.attempt}` : ""}`,
      });
    }

    if (segments.length === 0) {
      return null;
    }
    const minStartMs = Math.min(...segments.map((segment) => segment.startMs));
    const maxEndMs = Math.max(
      ...segments.map((segment) => segment.endMs),
      Number.isFinite(finishedMs) ? finishedMs : 0,
      isActiveRun ? nowMs : 0
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
  }, [invocationSessionRows, isActiveRun, logs, run]);
  const chatTimelineEntries = useMemo(() => {
    const stageUpdates = isRecord(run?.plan) && Array.isArray(run.plan["stage_updates"]) ? run.plan["stage_updates"] : [];
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
          kind: parsed.kind,
          ...(parsed.meta ? { meta: parsed.meta } : {})
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
      kind: entry.stage.toLowerCase() === "review" ? "error" : "status"
    }));

    const timeline = [...logEntries, ...diagnosticsEntries, ...stageUpdateEntries]
      .map((entry, index) => ({ entry, index }))
      .sort((a, b) => {
        const tsDiff = new Date(a.entry.recordedAt).getTime() - new Date(b.entry.recordedAt).getTime();
        if (tsDiff !== 0) {
          return tsDiff;
        }
        // Keep arrival/build order for same-timestamp entries so new events append.
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
  }, [logs, run?.created_at, run?.finished_at, run?.plan, run?.started_at, workflowDiagnostics?.history]);
  const visibleChatTimelineEntries = useMemo(
    () => chatTimelineEntries.slice(-Math.max(CHAT_PAGE_SIZE, chatVisibleCount)),
    [chatTimelineEntries, chatVisibleCount]
  );
  const hasOlderChatMessages = visibleChatTimelineEntries.length < chatTimelineEntries.length;
  const codePath = useMemo(() => {
    for (let idx = logs.length - 1; idx >= 0; idx -= 1) {
      const value = logs[idx]?.working_dir?.trim();
      if (value) {
        return value;
      }
    }
    return null;
  }, [logs]);
  useEffect(() => {
    setChatVisibleCount(CHAT_PAGE_SIZE);
    setChatAutoScroll(true);
  }, [params.runId]);
  useEffect(() => {
    if (activePanel !== "overview" || !chatAutoScroll) {
      return;
    }
    const listEl = chatListRef.current;
    if (!listEl) {
      return;
    }
    const raf = window.requestAnimationFrame(() => {
      listEl.scrollTop = listEl.scrollHeight;
    });
    return () => window.cancelAnimationFrame(raf);
  }, [activePanel, chatAutoScroll, visibleChatTimelineEntries.length]);

  return (
    <div className="space-y-6">
      {/* Page header — metadata strip */}
      <div className="space-y-3">
        {/* Row 1: status + id + chips; actions wrap on narrow screens */}
        <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
          {run ? <StatusBadge status={run.status} /> : null}
          <code className="max-w-full truncate rounded bg-muted px-2 py-0.5 text-xs font-mono">{run?.run_id ?? params.runId}</code>
          {run?.issue_key ? (
            run.issue_url ? (
              <Link
                href={run.issue_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-medium text-primary hover:bg-muted"
              >
                {run.issue_key}
                <ArrowLeft className="h-3 w-3 rotate-[135deg]" />
              </Link>
            ) : (
              <span className="rounded-md border px-2 py-0.5 text-xs font-medium">{run.issue_key}</span>
            )
          ) : null}
          {run?.pr_url ? (
            <Link
              href={run.pr_url}
              target="_blank"
              className="inline-flex items-center gap-1 rounded-md border border-primary/30 bg-primary/5 px-2 py-0.5 text-xs font-medium text-primary hover:bg-primary/10"
            >
              PR <ArrowLeft className="h-3 w-3 rotate-[135deg]" />
            </Link>
          ) : null}
          </div>
          <div className="flex min-h-9 flex-wrap items-center gap-1.5 sm:ml-auto">
            <Button variant="outline" size="sm" className="h-9 min-h-9 text-xs sm:h-7 sm:min-h-0" onClick={() => void loadRun()} disabled={busy}>
              {busy ? "Refreshing..." : "Refresh"}
            </Button>
            {isRerunnable ? (
              <details className="relative" data-testid="run-rerun-menu">
                <summary data-testid="run-rerun-trigger" className="flex h-9 min-h-9 cursor-pointer list-none items-center rounded-md border border-input bg-background px-3 text-xs text-foreground sm:h-7 sm:min-h-0">
                  {rerunBusy ? "Requeueing..." : "Rerun"}
                </summary>
                <div className="absolute right-0 z-20 mt-2 min-w-64 rounded-md border border-border bg-background p-1 shadow-lg">
                  {rerunOptions.map((option) => (
                    <button
                      key={option.key}
                      data-testid={`rerun-option-${option.key}`}
                      type="button"
                      className="flex w-full items-center justify-between rounded px-3 py-2 text-left text-xs hover:bg-muted"
                      onClick={() => void handleRerunSelection(option.payload, option.label)}
                      disabled={rerunBusy}
                    >
                      <span>{option.label}</span>
                      <span className="max-w-40 text-right text-[10px] text-muted-foreground">{option.detail}</span>
                    </button>
                  ))}
                </div>
              </details>
            ) : null}
            {isActiveRun ? (
              <Button variant="secondary" size="sm" className="h-9 min-h-9 text-xs sm:h-7 sm:min-h-0" onClick={() => void handleForceRerun()} disabled={forceRerunBusy}>
                {forceRerunBusy ? "Force rerunning..." : "Force Rerun"}
              </Button>
            ) : null}
            <Button asChild variant="outline" size="sm" className="h-9 min-h-9 text-xs sm:h-7 sm:min-h-0">
              <Link
                href={
                  run
                    ? (
                        projectContextId
                          ? `/${encodeURIComponent(run.tenant_id)}/projects/${encodeURIComponent(projectContextId)}/runs`
                          : `/${encodeURIComponent(run.tenant_id)}/runs`
                      )
                    : "/platform/dashboard"
                }
              >
                <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />
                Back
              </Link>
            </Button>
          </div>
        </div>

        {/* Row 2: metadata chips */}
        {run ? (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <span><span className="font-medium text-foreground">Tenant</span> {run.tenant_id}</span>
            {run.project_id ? <span><span className="font-medium text-foreground">Project</span> {run.project_id}</span> : null}
            {run.branch ? (
              <span data-testid="run-branch">
                <span className="font-medium text-foreground">Branch</span> <code className="rounded bg-muted px-1">{run.branch}</code>
              </span>
            ) : null}
            {executionContext.integration_branch && executionContext.integration_branch !== run.branch ? (
              <span data-testid="run-integration-branch">
                <span className="font-medium text-foreground">Integration branch</span>{" "}
                <code className="rounded bg-muted px-1">{executionContext.integration_branch}</code>
              </span>
            ) : null}
            {executionContext.execution_branch ? (
              <span data-testid="run-execution-branch">
                <span className="font-medium text-foreground">Execution branch</span>{" "}
                <code className="rounded bg-muted px-1">{executionContext.execution_branch}</code>
              </span>
            ) : null}
            <span><span className="font-medium text-foreground">Created</span> {formatTimestamp(run.created_at)}</span>
            {run.started_at ? <span><span className="font-medium text-foreground">Started</span> {formatTimestamp(run.started_at)}</span> : null}
            {run.finished_at ? <span><span className="font-medium text-foreground">Finished</span> {formatTimestamp(run.finished_at)}</span> : null}
            {run.status !== "queued" && run.status !== "running" ? (
              <span data-testid="run-not-active" className="rounded border border-border bg-muted px-1.5 py-0.5 text-[11px]">
                Not active
              </span>
            ) : null}
            {latestCodexSessionId ? (
              <span><span className="font-medium text-foreground">Session</span> <code className="rounded bg-muted px-1">{latestCodexSessionId}</code></span>
            ) : null}
          </div>
        ) : null}
      </div>

      {statusLine || (run && (run.status === "failed" || run.status === "blocked") && terminalFailureMessage) ? (
        <div className="space-y-4">
          {statusLine ? (
            <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-5 py-4 text-sm leading-relaxed text-destructive">
              {statusLine}
            </div>
          ) : null}
          {run && (run.status === "failed" || run.status === "blocked") && terminalFailureMessage ? (
            <div className="overflow-hidden rounded-2xl border border-destructive/30 bg-destructive/5">
              <div className="px-5 pt-5 sm:px-6 sm:pt-6">
                <h2 className="text-base font-semibold text-destructive">Failure Reason</h2>
              </div>
              <div className="space-y-3 px-5 pb-5 text-xs sm:px-6 sm:pb-6">
                <p className="break-words whitespace-pre-wrap text-destructive">{terminalFailureMessage}</p>
                {secondaryFailureDetail ? (
                  <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-destructive sm:p-4">
                    <p className="font-medium">Additional diagnostic context</p>
                    <p className="mt-2 break-words whitespace-pre-wrap">{secondaryFailureDetail}</p>
                  </div>
                ) : null}
                {terminalFailureHighlights.length > 0 ? (
                  <ul className="list-disc space-y-2 pl-5 text-destructive">
                    {terminalFailureHighlights.map((item, idx) => (
                      <li key={`failure-highlight-${idx}`} className="break-words whitespace-pre-wrap">
                        {item}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {run ? (
        <>
          {/* Pipeline stage bar */}
          <div className="flex items-center gap-2 overflow-x-auto">
            {(["pm", "dev", "test", "review"] as AgentStage[]).map((stage, idx) => {
              const progress = stageProgress[stage];
              const isRunning = progress.status === "running";
              const isDone = progress.status === "completed";
              const isInterrupted = progress.status === "interrupted";
              const statusDot = isRunning
                ? <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-warning" />
                : isDone
                  ? <span className="inline-block h-2 w-2 rounded-full bg-success" />
                  : isInterrupted
                    ? <span className="inline-block h-2 w-2 rounded-full bg-destructive" />
                    : <span className="inline-block h-2 w-2 rounded-full border border-muted-foreground/40 bg-muted" />;
              return (
                <div key={stage} className="flex items-center gap-2">
                  <div
                    data-testid={`run-stage-${stage}`}
                    data-stage-status={progress.status}
                    className="flex flex-col items-center gap-1 rounded-lg border px-3 py-2 text-xs"
                    style={{
                      borderColor: isInterrupted ? "rgb(239 68 68 / 0.35)" : isDone || isRunning ? stageColor(stage) + "60" : undefined,
                      backgroundColor: isInterrupted ? "rgb(239 68 68 / 0.08)" : isDone || isRunning ? stageColor(stage) + "10" : undefined,
                    }}
                  >
                    <div className="flex items-center gap-1.5">
                      {statusDot}
                      <span
                        className="font-semibold uppercase tracking-wide"
                        style={{ color: isInterrupted ? "rgb(220 38 38)" : isDone || isRunning ? stageColor(stage) : undefined }}
                      >
                        {stage}
                      </span>
                    </div>
                    <span data-testid={`run-stage-${stage}-detail`} className="text-[10px] text-muted-foreground">{progress.tileDetail}</span>
                  </div>
                  {idx < 3 ? <span className="text-muted-foreground/40">→</span> : null}
                </div>
              );
            })}
          </div>

          {/* Tab bar — underline style */}
          <div className="border-b overflow-x-auto">
            <nav className="-mb-px flex min-w-max gap-0" aria-label="Run detail panels">
              {PANEL_TABS.map((tab) => (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => setActivePanel(tab.id)}
                  className={[
                    "inline-flex items-center whitespace-nowrap border-b-2 px-4 py-2.5 text-sm font-medium transition-colors",
                    activePanel === tab.id
                      ? "border-primary text-foreground"
                      : "border-transparent text-muted-foreground hover:border-border hover:text-foreground"
                  ].join(" ")}
                >
                  {tab.label}
                </button>
              ))}
            </nav>
          </div>

          {/* Agents panel */}
          {activePanel === "agents" ? (
            <div className="overflow-hidden rounded-2xl border bg-background divide-y">
              {agentOutcomes.map((outcome) => (
                <div key={outcome.stage} className="px-5 py-4">
                  <div className="flex items-center gap-2">
                    <div
                      className="h-2.5 w-2.5 rounded-full"
                      style={{ backgroundColor: stageColor(outcome.stage) }}
                    />
                    <h2 className="text-sm font-semibold uppercase tracking-wide">
                      {outcome.label}
                    </h2>
                    <span className="ml-auto text-xs text-muted-foreground">
                      {stageProgress[outcome.stage as AgentStage]?.detail ?? "not started"}
                    </span>
                  </div>
                  <div className="mt-2 text-xs">
                    {outcome.items.length === 0 && !stageLiveSnapshots.get(outcome.stage as AgentStage) ? (
                      <p className="text-muted-foreground">{outcome.emptyText}</p>
                    ) : (
                      <div className="space-y-1">
                        {outcome.items.map((item, idx) => (
                          <p key={`${outcome.stage}-item-${idx}`} className="whitespace-pre-wrap">
                            {item}
                          </p>
                        ))}
                        {outcome.items.length === 0 && stageLiveSnapshots.get(outcome.stage as AgentStage) ? (
                          <p className="whitespace-pre-wrap text-muted-foreground">
                            Live snapshot ({new Date(stageLiveSnapshots.get(outcome.stage as AgentStage)!.recordedAt).toLocaleTimeString()}): {stageLiveSnapshots.get(outcome.stage as AgentStage)!.text}
                          </p>
                        ) : null}
                      </div>
                    )}
                    {outcome.feedback ? (
                      <p className="mt-2 rounded border border-warning/30 bg-warning/10 p-2 text-warning-foreground">
                        Feedback: {outcome.feedback}
                      </p>
                    ) : null}
                  </div>
                </div>
              ))}
            </div>
          ) : null}

          {/* Cost panel */}
          {activePanel === "cost" ? (
            <div className="space-y-4">
              {tokenTimelineBusy ? <p className="text-sm text-muted-foreground">Loading cost data...</p> : null}
              {!tokenTimelineBusy && tokenTimelineError ? (
                <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-5 py-4 text-sm leading-relaxed text-destructive">
                  {tokenTimelineError}
                </div>
              ) : null}
                {tokenTimelineBusy || !tokenTimeline ? null : (
                  <>
                    {/* KPI strip */}
                    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                      {[
                        { label: "Total Input", value: formatTokenCount(tokenTimeline.totals.input) },
                        { label: "Uncached Input", value: formatTokenCount(tokenTimeline.totals.uncached_input) },
                        { label: "Output", value: formatTokenCount(tokenTimeline.totals.output) },
                        { label: "Cache Ratio", value: `${(tokenTimeline.totals.cache_ratio * 100).toFixed(1)}%` },
                        { label: "Total I/O", value: formatTokenCount(tokenTimeline.totals.total_io) },
                        { label: "Avg Runtime", value: formatDuration(tokenTimeline.totals.avg_runtime_ms) },
                        { label: "P95 Runtime", value: formatDuration(tokenTimeline.totals.p95_runtime_ms) },
                        { label: "Turns", value: String(tokenTimeline.turns.length) }
                      ].map((kpi) => (
                        <div key={kpi.label} className="rounded-xl border bg-background p-4">
                          <p className="text-xs text-muted-foreground uppercase tracking-wide">{kpi.label}</p>
                          <p className="mt-1 text-2xl font-bold">{kpi.value}</p>
                        </div>
                      ))}
                    </div>
                    <div className="min-w-0 overflow-x-auto rounded-2xl border bg-background p-5">
                      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Token lane by turn</p>
                      <TokenStackedBarChart
                        data={tokenTimelineChartData}
                        xAxisKey="turnLabel"
                        bars={[
                          {
                            key: "cached_input_tokens",
                            label: "Cached input",
                            color: "#0ea5e9",
                            stackId: "tokenInput"
                          },
                          {
                            key: "uncached_input_tokens",
                            label: "Uncached input",
                            color: "#f59e0b",
                            stackId: "tokenInput"
                          },
                          {
                            key: "output_tokens",
                            label: "Output",
                            color: "#ef4444",
                            stackId: "tokenOutput"
                          }
                        ]}
                      />
                    </div>
                    <div className="rounded-2xl border bg-background p-5">
                      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Per-turn token breakdown
                      </p>
                      <div className="overflow-x-auto">
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead>Turn</TableHead>
                              <TableHead>Stage</TableHead>
                              <TableHead className="text-right">Attempt</TableHead>
                              <TableHead className="text-right">Input</TableHead>
                              <TableHead className="text-right">Cached</TableHead>
                              <TableHead className="text-right">Output</TableHead>
                              <TableHead className="text-right">Δ Input</TableHead>
                              <TableHead className="text-right">Δ Uncached</TableHead>
                              <TableHead className="text-right">Runtime</TableHead>
                              <TableHead>Growth Flags</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {tokenTurnRows.map((turn) => (
                              <TableRow
                                key={turn.rowKey}
                                className={turn.is_growth_spike ? "border-l-4 border-l-amber-500" : undefined}
                              >
                                <TableCell>{turn.turnOrder}</TableCell>
                                <TableCell className="whitespace-nowrap">{turn.stage.toUpperCase()}</TableCell>
                                <TableCell className="text-right">{turn.attempt === null ? "-" : turn.attempt}</TableCell>
                                <TableCell className="text-right">
                                  <div>{formatTokenCount(turn.input_tokens)}</div>
                                </TableCell>
                                <TableCell className="text-right">
                                  <div>{formatTokenCount(turn.cached_input_tokens)}</div>
                                  <div className="text-[11px] text-muted-foreground">
                                    uncached {formatTokenCount(turn.uncached_input_tokens)}
                                  </div>
                                </TableCell>
                                <TableCell className="text-right">{formatTokenCount(turn.output_tokens)}</TableCell>
                                <TableCell className="text-right">
                                  <div>{formatTokenCount(turn.delta_input)}</div>
                                  <div className="text-[11px] text-muted-foreground">vs prev</div>
                                </TableCell>
                                <TableCell className="text-right">
                                  <div>{formatTokenCount(turn.delta_uncached)}</div>
                                  <div className="text-[11px] text-muted-foreground">vs prev</div>
                                </TableCell>
                                <TableCell className="text-right">
                                  {turn.runtime_ms === null ? "n/a" : formatDuration(turn.runtime_ms)}
                                </TableCell>
                                <TableCell>
                                  {turn.is_growth_spike ? (
                                    <div className="space-y-1">
                                      {turn.spike_reason.map((reason) => (
                                        <span
                                          key={reason}
                                          className="mr-1 inline-block rounded-full bg-amber-100 px-2 py-0.5 text-[11px] text-amber-800"
                                        >
                                          {reason.replace(/_/g, " ")}
                                        </span>
                                      ))}
                                    </div>
                                  ) : (
                                    <span className="text-muted-foreground">None</span>
                                  )}
                                </TableCell>
                              </TableRow>
                            ))}
                            {tokenTurnRows.length === 0 ? (
                              <TableRow>
                                <TableCell colSpan={10} className="text-center text-muted-foreground">
                                  No token turns captured yet.
                                </TableCell>
                              </TableRow>
                            ) : null}
                          </TableBody>
                        </Table>
                      </div>
                    </div>
                  </>
                )}
              </div>
            ) : null}

          {/* Overview panel — run timeline + chat */}
          {activePanel === "overview" ? (
            <div className="space-y-4">
              {/* Gantt timeline */}
              {runTimeline ? (
                <div className="overflow-hidden rounded-2xl border bg-background">
                  <div className="px-5 pt-5">
                    <h2 className="text-sm font-semibold">Run Timeline</h2>
                  </div>
                  <div className="space-y-3 p-5">
                    <div className="grid gap-3 sm:grid-cols-4">
                      {[
                        { label: "Total", value: formatDuration(runTimeline.totalMs) },
                        { label: "Queue wait", value: formatDuration(runTimeline.queueWaitMs) },
                        { label: "Stage runtime", value: formatDuration(runTimeline.stageMs) },
                        { label: "Resumed", value: String(runTimeline.resumedCount) }
                      ].map((item) => (
                        <div key={item.label} className="rounded-xl border bg-background p-3 text-xs">
                          <p className="text-muted-foreground">{item.label}</p>
                          <p className="mt-0.5 font-semibold">{item.value}</p>
                        </div>
                      ))}
                    </div>
                    <div className="relative h-8 overflow-hidden rounded-lg bg-muted/50">
                      {runTimeline.segments.map((segment) => {
                        const leftPct = ((segment.startMs - runTimeline.minStartMs) / runTimeline.totalMs) * 100;
                        const widthPct = Math.max(1, (segment.durationMs / runTimeline.totalMs) * 100);
                        return (
                          <div
                            key={segment.key}
                            className="absolute top-0 h-8 text-[10px] font-semibold text-white"
                            style={{ left: `${leftPct}%`, width: `${widthPct}%`, backgroundColor: segment.color }}
                            title={`${segment.label}: ${formatDuration(segment.durationMs)} (${segment.detail})`}
                          >
                            <span className="block truncate px-1 pt-2">{segment.label}</span>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                </div>
              ) : null}

              {/* Chat timeline */}
              <div className="flex flex-col overflow-hidden rounded-2xl border bg-background">
                <div className="sticky top-0 z-10 flex items-center justify-between border-b bg-background px-5 py-3">
                  <h2 className="text-sm font-semibold">Activity Timeline</h2>
                  <div className="flex items-center gap-2">
                    <span className="text-[10px] text-muted-foreground">{chatTimelineEntries.length} messages</span>
                    {hasOlderChatMessages ? (
                      <Button variant="outline" size="sm" className="h-7 text-xs" onClick={() => { setChatVisibleCount((c) => Math.min(c + CHAT_PAGE_SIZE, chatTimelineEntries.length)); setChatAutoScroll(false); }}>
                        Load older
                      </Button>
                    ) : null}
                    {!chatAutoScroll ? (
                      <Button variant="outline" size="sm" className="h-7 text-xs" onClick={() => { setChatVisibleCount(CHAT_PAGE_SIZE); setChatAutoScroll(true); }}>
                        Latest
                      </Button>
                    ) : null}
                  </div>
                </div>
                <div className="flex-1 px-5 py-4">
                  {chatTimelineEntries.length === 0 ? (
                    <p className="py-8 text-center text-sm text-muted-foreground">No messages captured yet.</p>
                  ) : (
                    <ul ref={chatListRef} className="max-h-[560px] overflow-y-auto pr-1 text-xs">
                      {visibleChatTimelineEntries.map((entry, idx) => (
                        <TimelineRow
                          key={entry.key}
                          entry={entry}
                          stageDisplayLabelFn={stageDisplayLabel}
                          isLast={idx === visibleChatTimelineEntries.length - 1}
                        />
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            </div>
          ) : null}

          {/* Diagnostics panel — merged sessions + diagnostics + raw logs */}
          {activePanel === "diagnostics" ? (
            <div className="space-y-4">
              {/* Terminal diagnostics */}
              {workflowDiagnostics || terminalFailureMessage ? (
                <div className="overflow-hidden rounded-2xl border border-destructive/30 bg-destructive/5">
                  <div className="px-5 pt-5">
                    <h2 className="text-sm font-semibold text-destructive">Terminal Diagnostics</h2>
                  </div>
                  <div className="space-y-1 px-5 pb-5 text-xs">
                    <p><span className="font-medium">Stage:</span> {workflowDiagnostics?.stage || "unknown"}</p>
                    <p><span className="font-medium">Message:</span> {terminalFailureMessage || "No diagnostics message."}</p>
                  </div>
                </div>
              ) : null}

              <div className="overflow-hidden rounded-2xl border bg-background divide-y">
                {/* Session timeline */}
                <div className="px-5 py-5">
                  <h2 className="text-sm font-semibold">Codex Session Timeline</h2>
                  <div className="mt-3">
                    {invocationSessionRows.length === 0 ? (
                      <p className="text-xs text-muted-foreground">No stage invocation telemetry captured yet.</p>
                    ) : (
                      <ul className="max-h-[280px] space-y-2 overflow-y-auto pr-1 text-xs">
                        {invocationSessionRows.map((row) => (
                          <li key={row.key} className="rounded-lg border bg-background p-2.5">
                            <div className="flex items-center gap-2 mb-1">
                              <span className="font-semibold uppercase" style={{ color: stageColor(row.stage) }}>{row.stage}</span>
                              {row.attempt !== null ? <span className="text-muted-foreground">#{row.attempt}</span> : null}
                              <span className="rounded-full border px-1.5 py-0.5 text-[10px]">{row.resumedSession === null ? "unknown" : row.resumedSession ? "resumed" : "new"}</span>
                              {row.status ? <span className="text-muted-foreground">{row.status}</span> : null}
                              {row.durationMs !== null ? <span className="ml-auto text-muted-foreground">{formatDuration(row.durationMs)}</span> : null}
                            </div>
                            <p className="text-muted-foreground">start: {formatTimestamp(row.startedAt, "n/a")} · finish: {formatTimestamp(row.finishedAt, "n/a")}</p>
                            {row.codexSessionId ? <p className="break-all text-muted-foreground">session: {row.codexSessionId}</p> : null}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>

                {/* Agent events */}
                <div className="px-5 py-5">
                  <h2 className="text-sm font-semibold">Agent Events</h2>
                  <div className="mt-3">
                    {events.length === 0 ? (
                      <p className="text-xs text-muted-foreground">No agent events captured yet.</p>
                    ) : (
                      <ul className="max-h-[260px] space-y-2 overflow-y-auto pr-1 text-xs">
                        {events.map((event, idx) => (
                          <li key={`${event.agent_id}-${event.recorded_at}-${idx}`} className="rounded-lg border bg-background p-2.5">
                            <p><span className="font-medium">{event.event_type}</span> by {event.agent_id}</p>
                            <p className="text-muted-foreground" title={formatTimestamp(event.recorded_at)}>{formatTimeAgo(event.recorded_at)}</p>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>

                {/* Raw logs */}
                <div className="px-5 py-5">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <h2 className="text-sm font-semibold">Raw Agent Logs</h2>
                    <div className="flex items-center gap-1.5 text-xs">
                      <Button variant="outline" size="sm" className="h-7" onClick={() => void handleLoadOlderLogs()} disabled={loadingOlderLogs || !hasMoreLogs || logs.length === 0}>
                        {loadingOlderLogs ? "Loading..." : hasMoreLogs ? "Load older" : "All loaded"}
                      </Button>
                      {[
                        { label: "Agent", value: logAgentFilter, onChange: setLogAgentFilter, options: [["all", "All agents"], ["pm", "pm"], ["dev", "dev"], ["tester", "tester"], ["review", "review"]] },
                        { label: "Stage", value: logStageFilter, onChange: setLogStageFilter, options: [["all", "All stages"], ["pm", "pm"], ["dev", "dev"], ["test", "test"], ["review", "review"], ["orchestrated_run", "orchestrated_run"]] },
                        { label: "Stream", value: logStreamFilter, onChange: setLogStreamFilter, options: [["all", "All"], ["stdout", "stdout"], ["stderr", "stderr"]] }
                      ].map((filter) => (
                        <select key={filter.label} className="h-7 rounded border border-input bg-background px-2 text-xs" value={filter.value} onChange={(e) => filter.onChange(e.target.value)}>
                          {filter.options.map(([val, lbl]) => <option key={val} value={val}>{lbl}</option>)}
                        </select>
                      ))}
                    </div>
                  </div>
                  <div className="mt-3">
                    {logs.length === 0 ? (
                      <p className="text-xs text-muted-foreground">No logs captured yet.</p>
                    ) : filteredLogs.length === 0 ? (
                      <p className="text-xs text-muted-foreground">No log lines match current filters.</p>
                    ) : (
                      <ul className="max-h-[320px] space-y-2 overflow-y-auto pr-1 text-xs">
                        {filteredLogs.map((entry, idx) => (
                          <li key={`${entry.recorded_at}-${idx}`} className="rounded-lg border bg-background p-2.5" style={{ borderLeft: `2px solid ${stageColor(entry.stage)}` }}>
                            <p className="mb-0.5 text-muted-foreground">
                              <span className="font-medium text-foreground">{entry.agent_id}</span> · {entry.stage}{entry.attempt !== null ? ` #${entry.attempt}` : ""} [{entry.stream}] · <span title={formatTimestamp(entry.recorded_at)}>{formatTimeAgo(entry.recorded_at)}</span>
                            </p>
                            <p className="whitespace-pre-wrap">{entry.message}</p>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>

                {/* Plan JSON */}
                <div className="px-5 py-5">
                  <h2 className="text-sm font-semibold">Plan JSON</h2>
                  <div className="mt-3">
                    <pre className="max-h-[360px] overflow-auto rounded-lg bg-muted/50 p-3 text-xs whitespace-pre-wrap">
                      {run.plan ? JSON.stringify(run.plan, null, 2) : "No plan captured for this run."}
                    </pre>
                  </div>
                </div>
              </div>
            </div>
          ) : null}
        </>
      ) : (
        <p className="text-muted-foreground">Loading run details...</p>
      )}
    </div>
  );
}
