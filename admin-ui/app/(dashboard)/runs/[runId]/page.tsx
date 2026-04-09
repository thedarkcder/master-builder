"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, usePathname, useRouter } from "next/navigation";
import { ArrowLeft } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { TokenStackedBarChart } from "@/components/charts";
import {
  cancelRun,
  completeTeamTask,
  createWorkflowAttempt,
  getRun,
  getWorkflow,
  getTokenTimeline,
  listRunEvents,
  listRunLogs,
  streamRunEvents,
  submitTeamTaskApproval,
  type RunEventRecord,
  type RunLogEventRecord,
  type RunRecord,
  type WorkflowRecord,
  type WorkflowAttemptCreatePayload,
  type TokenTimelineRecord
} from "@/lib/api";
import { buildRunDetailPath, resolveRunRouteContext } from "@/lib/dashboard-paths";
import { buildChatTimelineEntries } from "@/lib/run-detail-projections/chat";
import { parseWorkflowDiagnostics } from "@/lib/run-detail-projections/diagnostics";
import {
  buildInvocationSessionRows,
  buildRunTimeline,
  type InvocationSessionRow,
} from "@/lib/run-detail-projections/timeline";
import {
  dedupeRunLogs,
  formatDuration,
  formatTokenCount,
  isAbortLikeError,
  normalizeTeamTaskStatus,
  parseExecutionContext,
  stageColor,
  stageDisplayLabel,
  statusFromLifecycleEvent,
  teamTaskStatusColor,
  teamTaskStatusLabel,
  toStringList,
  type TeamTaskProgressStatus,
} from "@/lib/run-detail-view-model";

type TeamTaskProgressEntry = {
  status: TeamTaskProgressStatus;
  tileDetail: string;
  detail: string;
};

type RunPanelTab = "overview" | "agents" | "diagnostics" | "cost";
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

export default function RunDetailPage() {
  const params = useParams<{ runId: string }>();
  const pathname = usePathname();
  const router = useRouter();
  const { credentials, ready } = useAuth();
  const routeContext = useMemo(() => resolveRunRouteContext(pathname), [pathname]);
  const [run, setRun] = useState<RunRecord | null>(null);
  const [workflow, setWorkflow] = useState<WorkflowRecord | null>(null);
  const [events, setEvents] = useState<RunEventRecord[]>([]);
  const [logs, setLogs] = useState<RunLogEventRecord[]>([]);
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
  const [teamActionKey, setTeamActionKey] = useState<string | null>(null);
  const projectContextId = routeContext.projectId ?? "";
  const teamRun = run?.team_run ?? null;
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
            const deduped = dedupeRunLogs([...prev, logEvent]).slice(-800);
            if (deduped.length === prev.length && deduped.every((entry, idx) => entry === prev[idx])) {
              return prev;
            }
            return deduped;
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

  async function handleForceRerun() {
    if (!credentials || !run) {
      return;
    }
    setForceRerunBusy(true);
    try {
      const restartCheckpointKind = latestCheckpointKind === "pm" ? "pm" : "execution";
      const cancelled = await cancelRun(credentials, run.run_id);
      const nextRun = await createWorkflowAttempt(credentials, cancelled.workflow_id, {
        mode: "restart",
        checkpoint_kind: restartCheckpointKind
      });
      setStatusLine(`Force-cancelled ${cancelled.run_id} and queued restart ${nextRun.run_id}.`);
      router.push(
        buildRunDetailPath({
          tenantId: run.tenant_id,
          projectId: projectContextId || null,
          runId: nextRun.run_id,
        })
      );
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
        return dedupeRunLogs([...prev, ...olderLogs]);
      });
      setHasMoreLogs(olderLogs.length >= 200);
    } catch (error) {
      setStatusLine(`Failed to load older logs: ${(error as Error).message}`);
    } finally {
      setLoadingOlderLogs(false);
    }
  }

  async function handleCompleteTeamTask(taskKey: string) {
    if (!credentials || !run) {
      return;
    }
    setTeamActionKey(`complete:${taskKey}`);
    try {
      const updated = await completeTeamTask(credentials, run.run_id, taskKey, {
        artifact_payload: {},
        summary: null,
      });
      setRun(updated);
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to complete team task: ${(error as Error).message}`);
    } finally {
      setTeamActionKey(null);
    }
  }

  async function handleApproveTeamTask(taskKey: string, decision: "approved" | "rejected") {
    if (!credentials || !run) {
      return;
    }
    setTeamActionKey(`approval:${taskKey}:${decision}`);
    try {
      const updated = await submitTeamTaskApproval(credentials, run.run_id, taskKey, {
        decision,
        comment: null,
      });
      setRun(updated);
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to submit team approval: ${(error as Error).message}`);
    } finally {
      setTeamActionKey(null);
    }
  }

  const isRerunnable = Boolean(run);
  const isActiveRun = run?.status === "queued" || run?.status === "running";
  const filteredLogs = useMemo(
    () =>
      logs.filter((entry) => {
        const normalizedAgentId = String(entry.agent_id ?? "").trim();
        const agentMatch =
          logAgentFilter === "all" ||
          normalizedAgentId === logAgentFilter ||
          entry.stage === logAgentFilter;
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
  const executionContext = useMemo(() => parseExecutionContext(run?.plan ?? null), [run?.plan]);
  const latestCheckpointKind = workflow?.latest_checkpoint_kind === "pm" || workflow?.latest_checkpoint_kind === "execution"
    ? workflow.latest_checkpoint_kind
    : null;
  const invocationSessionRows = useMemo<InvocationSessionRow[]>(() => buildInvocationSessionRows(logs), [logs]);
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
    if (latestCheckpointKind !== null) {
      options.push({
        key: `resume-${latestCheckpointKind}`,
        label: "Resume latest checkpoint",
        payload: { mode: "resume", checkpoint_kind: latestCheckpointKind },
        detail: `Continue from the latest ${latestCheckpointKind} checkpoint.`
      });
      options.push({
        key: `restart-${latestCheckpointKind}`,
        label: "Restart from latest checkpoint",
        payload: { mode: "restart", checkpoint_kind: latestCheckpointKind },
        detail: `Create a new attempt from the latest ${latestCheckpointKind} checkpoint.`
      });
    }
    return options;
  }, [run, latestCheckpointKind]);

  async function handleRerunSelection(payload: WorkflowAttemptCreatePayload, label: string) {
    if (!credentials || !run) {
      return;
    }
    setRerunBusy(true);
    try {
      const nextRun = await createWorkflowAttempt(credentials, run.workflow_id, payload);
      setStatusLine(`Queued ${label.toLowerCase()} as run ${nextRun.run_id} for ${nextRun.issue_key}.`);
      router.push(
        buildRunDetailPath({
          tenantId: run.tenant_id,
          projectId: projectContextId || null,
          runId: nextRun.run_id,
        })
      );
    } catch (error) {
      setStatusLine(`Failed to rerun: ${(error as Error).message}`);
    } finally {
      setRerunBusy(false);
    }
  }
  const workflowDiagnostics = useMemo(() => parseWorkflowDiagnostics(run), [run]);
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
      .map((entry) => entry.event.trim())
      .filter((entry) => entry.length > 0);
    return items.slice(0, 6);
  }, [workflowDiagnostics?.history]);
  const teamTaskProgress = useMemo(() => {
    const progress = new Map<string, TeamTaskProgressEntry>();
    if (!teamRun) {
      return progress;
    }
    for (const node of teamRun.nodes) {
      const normalized = normalizeTeamTaskStatus(node.status);
      progress.set(node.task_key, {
        status: normalized,
        tileDetail: teamTaskStatusLabel(normalized),
        detail: teamTaskStatusLabel(normalized),
      });
    }
    return progress;
  }, [teamRun]);
  const teamAgentOutcomes = useMemo(() => {
    if (!teamRun) {
      return [];
    }
    return teamRun.nodes.map((node) => {
      const producedArtifacts = toStringList(node.artifact_contract?.["produces"]);
      const consumedArtifacts = toStringList(node.artifact_contract?.["consumes"]);
      const dependencies = node.dependency_keys.length > 0 ? node.dependency_keys.join(", ") : "none";
      const approvalMode = String(node.approval_rule?.["type"] ?? "").trim();
      const items = [
        `Role: ${node.owner_role_key}`,
        node.owner_persona_key ? `Persona: ${node.owner_persona_key}` : "",
        node.owner_agent_key ? `Agent: ${node.owner_agent_key}` : "",
        `Depends on: ${dependencies}`,
        producedArtifacts.length > 0 ? `Produces: ${producedArtifacts.join(", ")}` : "",
        consumedArtifacts.length > 0 ? `Consumes: ${consumedArtifacts.join(", ")}` : "",
        approvalMode ? `Approval: ${approvalMode}` : "",
      ].filter((item) => item.length > 0);
      return {
        key: node.task_key,
        label: node.label,
        status: teamTaskProgress.get(node.task_key)?.detail ?? teamTaskStatusLabel(normalizeTeamTaskStatus(node.status)),
        ownerRoleKey: node.owner_role_key,
        ownerPersonaKey: node.owner_persona_key,
        ownerAgentKey: node.owner_agent_key,
        items,
      };
    });
  }, [teamRun, teamTaskProgress]);
  const teamSummary = useMemo(() => {
    if (!teamRun) {
      return null;
    }
    const completed = teamRun.nodes.filter((node) => normalizeTeamTaskStatus(node.status) === "completed").length;
    const running = teamRun.nodes.filter((node) => normalizeTeamTaskStatus(node.status) === "running").length;
    const blocked = teamRun.nodes.filter((node) => normalizeTeamTaskStatus(node.status) === "blocked").length;
    return {
      completed,
      running,
      blocked,
      pending: Math.max(0, teamRun.nodes.length - completed - running - blocked),
    };
  }, [teamRun]);
  const logAgentOptions = useMemo(() => {
    const options = Array.from(
      new Set([
        ...(teamRun?.nodes.map((node) => node.owner_agent_key).filter((value): value is string => Boolean(value && value.trim())) ?? []),
        ...logs.map((entry) => String(entry.agent_id ?? "").trim()).filter((value) => value.length > 0),
      ]),
    )
      .sort()
      .map((value) => [value, value] as [string, string]);
    return [["all", "All agents"], ...options];
  }, [logs, teamRun]);
  const logStageOptions = useMemo(() => {
    const options = Array.from(
      new Set([
        ...(teamRun?.nodes.map((node) => node.task_key) ?? []),
        ...logs.map((entry) => String(entry.stage ?? "").trim()).filter((value) => value.length > 0),
      ]),
    )
      .sort()
      .map((value) => [value, value] as [string, string]);
    return [["all", teamRun ? "All tasks" : "All stages"], ...options];
  }, [logs, teamRun]);
  const runTimeline = useMemo(
    () => buildRunTimeline({ run, logs, invocationSessionRows, isActiveRun }),
    [invocationSessionRows, isActiveRun, logs, run]
  );
  const chatTimelineEntries = useMemo(
    () => buildChatTimelineEntries({ run, logs, workflowDiagnostics }),
    [logs, run, workflowDiagnostics]
  );
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
    <div className="space-y-0">
      {/* Page header — metadata strip */}
      <div className="mb-6 space-y-3">
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
                    : "/dashboard"
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
            <span><span className="font-medium text-foreground">Created</span> {new Date(run.created_at).toLocaleString()}</span>
            {run.started_at ? <span><span className="font-medium text-foreground">Started</span> {new Date(run.started_at).toLocaleString()}</span> : null}
            {run.finished_at ? <span><span className="font-medium text-foreground">Finished</span> {new Date(run.finished_at).toLocaleString()}</span> : null}
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
        <div className="mb-8 space-y-4">
          {statusLine ? (
            <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-5 py-4 text-sm leading-relaxed text-destructive">
              {statusLine}
            </div>
          ) : null}
          {run && (run.status === "failed" || run.status === "blocked") && terminalFailureMessage ? (
            <Card className="border-destructive/40 bg-destructive/5 shadow-sm">
              <CardHeader className="space-y-1 px-5 pb-3 pt-5 sm:px-6 sm:pt-6">
                <CardTitle className="text-base font-semibold text-destructive">Failure Reason</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 px-5 pb-5 text-xs sm:px-6 sm:pb-6">
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
              </CardContent>
            </Card>
          ) : null}
        </div>
      ) : null}

      {run ? (
        <>
          {/* Team run graph strip */}
          {teamRun ? (
            <div className="mb-6 mt-1 space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold text-foreground">{teamRun.team_label}</span>
                <span className="rounded-full border px-2 py-0.5 text-[11px] text-muted-foreground">
                  v{teamRun.definition_version}
                </span>
                <span className="rounded-full border px-2 py-0.5 text-[11px] text-muted-foreground">
                  {teamRun.status}
                </span>
              </div>
              <div className="flex items-center gap-2 overflow-x-auto">
                {teamRun.nodes.map((node, idx) => {
                  const progress = teamTaskProgress.get(node.task_key) ?? {
                    status: "not_started" as TeamTaskProgressStatus,
                    tileDetail: "pending",
                    detail: "pending",
                  };
                  const dependencyLabel = node.dependency_keys.length > 0
                    ? `Depends on: ${node.dependency_keys.join(", ")}`
                    : "Start task";
                  const isRunning = progress.status === "running";
                  const isDone = progress.status === "completed";
                  const isBlocked = progress.status === "blocked";
                  const color = teamTaskStatusColor(progress.status);
                  const statusDot = isRunning
                    ? <span className="inline-block h-2 w-2 animate-pulse rounded-full" style={{ backgroundColor: color }} />
                    : isDone || isBlocked
                      ? <span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: color }} />
                      : <span className="inline-block h-2 w-2 rounded-full border border-muted-foreground/40 bg-muted" />;
                  return (
                    <div key={node.task_key} className="flex items-center gap-2">
                      <div
                        data-testid={`team-run-node-${node.task_key}`}
                        className="flex min-w-[10rem] flex-col gap-1 rounded-lg border px-3 py-2 text-xs"
                        style={{
                          borderColor: isDone || isRunning || isBlocked ? `${color}60` : undefined,
                          backgroundColor: isDone || isRunning || isBlocked ? `${color}10` : undefined,
                        }}
                      >
                        <div className="flex items-center gap-1.5">
                          {statusDot}
                          <span className="font-semibold">{node.label}</span>
                        </div>
                        <span className="text-[10px] text-muted-foreground">{progress.tileDetail}</span>
                        <span className="text-[10px] text-muted-foreground">
                          {node.owner_role_key}
                          {node.owner_persona_key ? ` · ${node.owner_persona_key}` : ""}
                        </span>
                        <span
                          data-testid={`team-run-node-dependencies-${node.task_key}`}
                          className="text-[10px] text-muted-foreground"
                        >
                          {dependencyLabel}
                        </span>
                      </div>
                      {idx < teamRun.nodes.length - 1 ? <span className="text-muted-foreground/40">•</span> : null}
                    </div>
                  );
                })}
              </div>
            </div>
          ) : (
            <div className="mb-6 rounded-lg border border-amber-300/60 bg-amber-100/60 px-4 py-3 text-xs text-amber-900">
              This run does not include a team definition snapshot and is no longer supported in the dynamic team-run UI.
            </div>
          )}

          {/* Tab bar — underline style */}
          <div className="mb-6 border-b overflow-x-auto">
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
            <div className="space-y-3">
              {teamRun ? teamAgentOutcomes.map((outcome) => {
                const progress = teamTaskProgress.get(outcome.key) ?? {
                  status: "not_started" as TeamTaskProgressStatus,
                  tileDetail: "pending",
                  detail: "pending",
                };
                return (
                  <Card key={outcome.key}>
                    <CardHeader className="pb-2 pt-4">
                      <div className="flex items-center gap-2">
                        <div
                          className="h-2.5 w-2.5 rounded-full"
                          style={{ backgroundColor: teamTaskStatusColor(progress.status) }}
                        />
                        <CardTitle className="text-sm font-semibold tracking-wide">
                          {outcome.label}
                        </CardTitle>
                        <span className="text-xs text-muted-foreground">{outcome.ownerRoleKey}</span>
                        <span className="ml-auto text-xs text-muted-foreground">{outcome.status}</span>
                      </div>
                    </CardHeader>
                    <CardContent className="space-y-1 text-xs">
                      {outcome.items.map((item, idx) => (
                        <p key={`${outcome.key}-item-${idx}`} className="whitespace-pre-wrap">
                          {item}
                        </p>
                      ))}
                      <div className="flex flex-wrap gap-2 pt-2">
                        {(teamRun.nodes.find((node) => node.task_key === outcome.key)?.status === "ready") ? (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 text-xs"
                            onClick={() => void handleCompleteTeamTask(outcome.key)}
                            disabled={teamActionKey === `complete:${outcome.key}`}
                          >
                            {teamActionKey === `complete:${outcome.key}` ? "Completing..." : "Complete task"}
                          </Button>
                        ) : null}
                        {(teamRun.nodes.find((node) => node.task_key === outcome.key)?.status === "awaiting_approval") ? (
                          <>
                            <Button
                              size="sm"
                              className="h-7 text-xs"
                              onClick={() => void handleApproveTeamTask(outcome.key, "approved")}
                              disabled={teamActionKey === `approval:${outcome.key}:approved` || teamActionKey === `approval:${outcome.key}:rejected`}
                            >
                              {teamActionKey === `approval:${outcome.key}:approved` ? "Approving..." : "Approve"}
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              className="h-7 text-xs"
                              onClick={() => void handleApproveTeamTask(outcome.key, "rejected")}
                              disabled={teamActionKey === `approval:${outcome.key}:approved` || teamActionKey === `approval:${outcome.key}:rejected`}
                            >
                              {teamActionKey === `approval:${outcome.key}:rejected` ? "Rejecting..." : "Reject"}
                            </Button>
                          </>
                        ) : null}
                      </div>
                    </CardContent>
                  </Card>
                );
              }) : (
                <Card>
                  <CardContent className="p-4 text-sm text-muted-foreground">
                    Legacy non-team runs are no longer rendered in the Agents panel.
                  </CardContent>
                </Card>
              )}
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
                        <Card key={kpi.label}>
                          <CardContent className="p-4">
                            <p className="text-xs text-muted-foreground uppercase tracking-wide">{kpi.label}</p>
                            <p className="mt-1 text-2xl font-bold">{kpi.value}</p>
                          </CardContent>
                        </Card>
                      ))}
                    </div>
                    <div className="min-w-0 overflow-x-auto rounded-md border bg-muted/20 p-3">
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
                    <div className="rounded-md border bg-muted/20 p-3">
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
              {teamRun && teamSummary ? (
                <Card>
                  <CardHeader className="pb-3">
                    <CardTitle className="text-sm">{teamRun.team_label} Overview</CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <div className="grid gap-3 sm:grid-cols-4">
                      {[
                        { label: "Completed", value: String(teamSummary.completed) },
                        { label: "Running", value: String(teamSummary.running) },
                        { label: "Pending", value: String(teamSummary.pending) },
                        { label: "Blocked", value: String(teamSummary.blocked) },
                      ].map((item) => (
                        <div key={item.label} className="rounded-lg border p-3 text-xs">
                          <p className="text-muted-foreground">{item.label}</p>
                          <p className="mt-0.5 font-semibold">{item.value}</p>
                        </div>
                      ))}
                    </div>
                    <div className="grid gap-3 lg:grid-cols-[1.6fr,1fr]">
                      <div className="rounded-lg border p-3 text-xs">
                        <p className="mb-2 font-medium text-foreground">Task graph</p>
                        <div className="space-y-2">
                          {teamRun.nodes.map((node) => (
                            <div key={node.task_key} className="flex items-start justify-between gap-3 rounded-md border bg-muted/20 px-3 py-2">
                              <div className="min-w-0">
                                <p className="font-medium">{node.label}</p>
                                <p className="text-muted-foreground">
                                  {node.owner_role_key}
                                  {node.owner_agent_key ? ` · ${node.owner_agent_key}` : ""}
                                </p>
                              </div>
                              <span className="text-muted-foreground">
                                {node.dependency_keys.length > 0 ? node.dependency_keys.join(", ") : "root"}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                      <div className="rounded-lg border p-3 text-xs">
                        <p className="mb-2 font-medium text-foreground">Coordination state</p>
                        <div className="space-y-2 text-muted-foreground">
                          <p>Artifacts: {teamRun.artifacts.length}</p>
                          <p>Approvals: {teamRun.approvals.length}</p>
                          <p>Edges: {teamRun.edges.length}</p>
                          <p>Status: {teamRun.status}</p>
                        </div>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              ) : null}
              {/* Gantt timeline */}
              {runTimeline ? (
                <Card>
                  <CardHeader className="pb-3">
                    <CardTitle className="text-sm">Run Timeline</CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <div className="grid gap-3 sm:grid-cols-4">
                      {[
                        { label: "Total", value: formatDuration(runTimeline.totalMs) },
                        { label: "Queue wait", value: formatDuration(runTimeline.queueWaitMs) },
                        { label: "Stage runtime", value: formatDuration(runTimeline.stageMs) },
                        { label: "Resumed", value: String(runTimeline.resumedCount) }
                      ].map((item) => (
                        <div key={item.label} className="rounded-lg border p-3 text-xs">
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
                  </CardContent>
                </Card>
              ) : null}

              {/* Chat timeline */}
              <Card>
                <CardHeader className="pb-3">
                  <div className="flex items-center justify-between">
                    <CardTitle className="text-sm">Activity Timeline</CardTitle>
                    <div className="flex gap-1.5">
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
                </CardHeader>
                <CardContent>
                  {chatTimelineEntries.length === 0 ? (
                    <p className="text-sm text-muted-foreground">No messages captured yet.</p>
                  ) : (
                    <ul ref={chatListRef} className="max-h-[480px] space-y-1.5 overflow-y-auto pr-1 text-xs">
                      {visibleChatTimelineEntries.map((entry) => {
                        if (entry.kind === "status") {
                          return (
                            <li key={entry.key} className="flex items-center gap-2 py-1">
                              <div className="h-px flex-1 bg-border" />
                              <span className="max-w-[min(100%,56rem)] whitespace-normal break-words rounded-xl border px-2 py-1 text-center text-[10px] text-muted-foreground">
                                {stageDisplayLabel(entry.stage)}{entry.attempt !== null ? ` #${entry.attempt}` : ""} — {entry.text}
                              </span>
                              <div className="h-px flex-1 bg-border" />
                            </li>
                          );
                        }
                        if (entry.kind === "error") {
                          return (
                            <li
                              key={entry.key}
                              className="rounded-lg border-l-2 border-destructive bg-destructive/5 p-3 sm:p-4"
                            >
                              <p className="mb-2 text-[10px] text-muted-foreground">
                                {new Date(entry.recordedAt).toLocaleString()} · {stageDisplayLabel(entry.stage)} · {entry.speaker}
                              </p>
                              <p className="break-words whitespace-pre-wrap text-destructive">{entry.text}</p>
                            </li>
                          );
                        }
                        if (entry.kind === "reasoning") {
                          return (
                            <li key={entry.key}>
                              <details className="rounded-lg border bg-muted/30 p-2 text-[10px] text-muted-foreground">
                                <summary className="cursor-pointer font-medium">
                                  {stageDisplayLabel(entry.stage)} reasoning · {new Date(entry.recordedAt).toLocaleTimeString()}
                                </summary>
                                <p className="mt-1.5 whitespace-pre-wrap italic">{entry.text}</p>
                              </details>
                            </li>
                          );
                        }
                        return (
                          <li
                            key={entry.key}
                            className="rounded-lg bg-primary/5 p-2.5"
                            style={{ borderLeft: `2px solid ${stageColor(entry.stage)}` }}
                          >
                            <p className="mb-1 text-[10px] text-muted-foreground">
                              {new Date(entry.recordedAt).toLocaleString()} · {stageDisplayLabel(entry.stage)}{entry.attempt !== null ? ` #${entry.attempt}` : ""} · {entry.speaker}
                            </p>
                            <p className="whitespace-pre-wrap">{entry.text}</p>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </CardContent>
              </Card>
            </div>
          ) : null}

          {/* Diagnostics panel — merged sessions + diagnostics + raw logs */}
          {activePanel === "diagnostics" ? (
            <div className="space-y-4">
              {/* Terminal diagnostics */}
              {workflowDiagnostics || terminalFailureMessage ? (
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm">Terminal Diagnostics</CardTitle>
                  </CardHeader>
                  <CardContent className="text-xs space-y-1">
                    <p><span className="font-medium">Stage:</span> {workflowDiagnostics?.stage || "unknown"}</p>
                    <p><span className="font-medium">Message:</span> {terminalFailureMessage || "No diagnostics message."}</p>
                  </CardContent>
                </Card>
              ) : null}

              {/* Session timeline */}
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Codex Session Timeline</CardTitle>
                </CardHeader>
                <CardContent>
                  {invocationSessionRows.length === 0 ? (
                    <p className="text-xs text-muted-foreground">No stage invocation telemetry captured yet.</p>
                  ) : (
                    <ul className="max-h-[280px] space-y-2 overflow-y-auto pr-1 text-xs">
                      {invocationSessionRows.map((row) => (
                        <li key={row.key} className="rounded-lg border p-2.5">
                          <div className="flex items-center gap-2 mb-1">
                            <span className="font-semibold uppercase" style={{ color: stageColor(row.stage) }}>{row.stage}</span>
                            {row.attempt !== null ? <span className="text-muted-foreground">#{row.attempt}</span> : null}
                            <span className="rounded-full border px-1.5 py-0.5 text-[10px]">{row.resumedSession === null ? "unknown" : row.resumedSession ? "resumed" : "new"}</span>
                            {row.status ? <span className="text-muted-foreground">{row.status}</span> : null}
                            {row.durationMs !== null ? <span className="ml-auto text-muted-foreground">{formatDuration(row.durationMs)}</span> : null}
                          </div>
                          <p className="text-muted-foreground">start: {row.startedAt ? new Date(row.startedAt).toLocaleString() : "n/a"} · finish: {row.finishedAt ? new Date(row.finishedAt).toLocaleString() : "n/a"}</p>
                          {row.codexSessionId ? <p className="break-all text-muted-foreground">session: {row.codexSessionId}</p> : null}
                        </li>
                      ))}
                    </ul>
                  )}
                </CardContent>
              </Card>

              {/* Agent events */}
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Agent Events</CardTitle>
                </CardHeader>
                <CardContent>
                  {events.length === 0 ? (
                    <p className="text-xs text-muted-foreground">No agent events captured yet.</p>
                  ) : (
                    <ul className="max-h-[260px] space-y-2 overflow-y-auto pr-1 text-xs">
                      {events.map((event, idx) => (
                        <li key={`${event.agent_id}-${event.recorded_at}-${idx}`} className="rounded-lg border p-2.5">
                          <p><span className="font-medium">{event.event_type}</span> by {event.agent_id}</p>
                          <p className="text-muted-foreground">{new Date(event.recorded_at).toLocaleString()}</p>
                        </li>
                      ))}
                    </ul>
                  )}
                </CardContent>
              </Card>

              {/* Raw logs */}
              <Card>
                <CardHeader className="pb-2">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <CardTitle className="text-sm">Raw Agent Logs</CardTitle>
                    <div className="flex items-center gap-1.5 text-xs">
                      <Button variant="outline" size="sm" className="h-7" onClick={() => void handleLoadOlderLogs()} disabled={loadingOlderLogs || !hasMoreLogs || logs.length === 0}>
                        {loadingOlderLogs ? "Loading..." : hasMoreLogs ? "Load older" : "All loaded"}
                      </Button>
                      {[
                        { label: "Agent", value: logAgentFilter, onChange: setLogAgentFilter, options: logAgentOptions },
                        { label: "Stage", value: logStageFilter, onChange: setLogStageFilter, options: logStageOptions },
                        { label: "Stream", value: logStreamFilter, onChange: setLogStreamFilter, options: [["all", "All"], ["stdout", "stdout"], ["stderr", "stderr"]] }
                      ].map((filter) => (
                        <select key={filter.label} className="h-7 rounded border border-input bg-background px-2 text-xs" value={filter.value} onChange={(e) => filter.onChange(e.target.value)}>
                          {filter.options.map(([val, lbl]) => <option key={val} value={val}>{lbl}</option>)}
                        </select>
                      ))}
                    </div>
                  </div>
                </CardHeader>
                <CardContent>
                  {logs.length === 0 ? (
                    <p className="text-xs text-muted-foreground">No logs captured yet.</p>
                  ) : filteredLogs.length === 0 ? (
                    <p className="text-xs text-muted-foreground">No log lines match current filters.</p>
                  ) : (
                    <ul className="max-h-[320px] space-y-2 overflow-y-auto pr-1 text-xs">
                      {filteredLogs.map((entry, idx) => (
                        <li key={`${entry.recorded_at}-${idx}`} className="rounded-lg border p-2.5" style={{ borderLeft: `2px solid ${stageColor(entry.stage)}` }}>
                          <p className="mb-0.5 text-muted-foreground">
                            <span className="font-medium text-foreground">{entry.agent_id}</span> · {entry.stage}{entry.attempt !== null ? ` #${entry.attempt}` : ""} [{entry.stream}] · {new Date(entry.recorded_at).toLocaleString()}
                          </p>
                          <p className="whitespace-pre-wrap">{entry.message}</p>
                        </li>
                      ))}
                    </ul>
                  )}
                </CardContent>
              </Card>

              {/* Plan JSON */}
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">Plan JSON</CardTitle>
                </CardHeader>
                <CardContent>
                  <pre className="max-h-[360px] overflow-auto rounded-lg bg-muted/50 p-3 text-xs whitespace-pre-wrap">
                    {run.plan ? JSON.stringify(run.plan, null, 2) : "No plan captured for this run."}
                  </pre>
                </CardContent>
              </Card>
            </div>
          ) : null}
        </>
      ) : (
        <p className="text-muted-foreground">Loading run details...</p>
      )}
    </div>
  );
}
