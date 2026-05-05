"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { Clock3, RefreshCw, X } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { formatTimeAgo } from "@/lib/datetime";
import {
  listWorkflowBoardItems,
  startWorkflowExecution,
  type WorkflowBoardItemRecord,
  type WorkflowBoardRunSummaryRecord,
} from "@/lib/api";
import { useToast } from "@/components/ui/toast-provider";

type BoardLaneKey = "needs_input" | "planning" | "ready" | "engineering" | "done";

type BoardLane = {
  key: BoardLaneKey;
  title: string;
};

type ParentWorkCard = {
  workflow: WorkflowBoardItemRecord;
  lane: BoardLaneKey;
  issueKey: string;
  title: string;
  questionOpen: boolean;
  runCount: number;
  activeRunCount: number;
  failedRunCount: number;
  latestRun: WorkflowBoardRunSummaryRecord | null;
};

const BOARD_LANES: BoardLane[] = [
  { key: "needs_input", title: "Needs input" },
  { key: "planning", title: "Planning" },
  { key: "ready", title: "Ready to start" },
  { key: "engineering", title: "Engineering active" },
  { key: "done", title: "Done" },
];

const ACTIVE_RUN_STATUSES = new Set(["queued", "running", "processing", "retrying", "review", "in_review", "blocked", "failed"]);
const TERMINAL_SUCCESS_RUN_STATUSES = new Set(["succeeded", "completed", "done"]);

export function isParentPlanningWorkflow(workflow: WorkflowBoardItemRecord): boolean {
  return (
    workflow.workflow_type_key === "parent_planning" ||
    workflow.dedupe_scope === "parent_planning" ||
    workflow.workflow_id.startsWith("parent_planning:")
  );
}

export function isJiraProjectReconciliationWorkflow(workflow: WorkflowBoardItemRecord): boolean {
  return (
    workflow.workflow_type_key === "jira_project_reconciliation" ||
    workflow.dedupe_scope === "jira_project_reconciliation" ||
    workflow.workflow_id.startsWith("jira_project_reconciliation:")
  );
}

export function normalizedWorkflowStatus(value: string | null | undefined): string {
  return String(value || "").trim().toLowerCase();
}

export function latestWorkflowActivity(workflow: WorkflowBoardItemRecord): string {
  const candidates = [
    workflow.latest_activity_at,
    workflow.updated_at,
    workflow.finished_at,
    workflow.started_at,
    workflow.created_at,
    ...workflow.runs.flatMap((run) => [run.finished_at, run.started_at, run.created_at]),
  ].filter(Boolean) as string[];
  return [...candidates].sort().at(-1) ?? workflow.created_at;
}

export function workflowLane(workflow: WorkflowBoardItemRecord): BoardLaneKey {
  const status = normalizedWorkflowStatus(workflow.status);
  const runs = workflow.runs ?? [];
  const runStatuses = runs.map((run) => normalizedWorkflowStatus(run.status));
  const hasActiveRun = runStatuses.some((runStatus) => ACTIVE_RUN_STATUSES.has(runStatus));
  const allRunsCompleted = runs.length > 0 && runStatuses.every((runStatus) => TERMINAL_SUCCESS_RUN_STATUSES.has(runStatus));

  if (status === "waiting_for_input" || workflow.pending_input_request_id) {
    return "needs_input";
  }
  if (status === "failed" || status === "blocked") {
    return "needs_input";
  }
  if (hasActiveRun) {
    return "engineering";
  }
  if (allRunsCompleted) {
    return "done";
  }
  if (status === "completed") {
    return "ready";
  }
  return "planning";
}

function buildParentWorkCard(workflow: WorkflowBoardItemRecord): ParentWorkCard {
  const sortedRuns = [...(workflow.runs ?? [])].sort((left, right) => {
    const leftActivity = left.finished_at ?? left.started_at ?? left.created_at;
    const rightActivity = right.finished_at ?? right.started_at ?? right.created_at;
    return rightActivity.localeCompare(leftActivity);
  });
  const activeRunCount = sortedRuns.filter((run) => ACTIVE_RUN_STATUSES.has(normalizedWorkflowStatus(run.status))).length;
  const failedRunCount = sortedRuns.filter((run) => normalizedWorkflowStatus(run.status) === "failed").length;
  return {
    workflow,
    lane: workflowLane(workflow),
    issueKey: workflow.source_ref || workflow.workflow_id,
    title: workflow.display_name || workflow.source_ref || workflow.workflow_id,
    questionOpen: Boolean(workflow.pending_input_request_id || normalizedWorkflowStatus(workflow.status) === "waiting_for_input"),
    runCount: workflow.run_count,
    activeRunCount,
    failedRunCount,
    latestRun: workflow.latest_run ?? sortedRuns[0] ?? null,
  };
}

export function ProjectParentWorkBoard({
  tenantId,
  projectId,
  allowJiraReconciliation,
}: {
  tenantId: string;
  projectId: string;
  allowJiraReconciliation: boolean;
}) {
  const { credentials, ready } = useAuth();
  const { showToast } = useToast();
  const [allWorkflows, setAllWorkflows] = useState<WorkflowBoardItemRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [selectedCard, setSelectedCard] = useState<ParentWorkCard | null>(null);

  useEffect(() => {
    let disposed = false;
    async function loadProjectWork() {
      if (!ready || !credentials) return;
      setLoading(true);
      setErrorMessage(null);
      try {
        const workflowPayload = await listWorkflowBoardItems(credentials, { tenantId, projectId, limit: 100 });
        if (disposed) return;
        setAllWorkflows(
          workflowPayload.sort((left, right) => latestWorkflowActivity(right).localeCompare(latestWorkflowActivity(left))),
        );
      } catch (error) {
        if (!disposed) {
          setErrorMessage(`Failed to load project parent work: ${(error as Error).message}`);
        }
      } finally {
        if (!disposed) {
          setLoading(false);
        }
      }
    }
    void loadProjectWork();
    return () => {
      disposed = true;
    };
  }, [credentials, projectId, ready, tenantId]);

  async function syncJiraWork() {
    if (!credentials || syncing) return;
    setSyncing(true);
    setErrorMessage(null);
    try {
      const result = await startWorkflowExecution(credentials, {
        workflow_type_key: "jira_project_reconciliation",
        tenant_id: tenantId,
        project_id: projectId,
        input: { max_items: 1000 },
      });
      const workflowPayload = await listWorkflowBoardItems(credentials, { tenantId, projectId, limit: 100 });
      setAllWorkflows(
        workflowPayload.sort((left, right) => latestWorkflowActivity(right).localeCompare(latestWorkflowActivity(left))),
      );
      showToast({
        title: "Jira reconciliation started",
        description: `Workflow execution ${result.execution_id} is ${result.status}.`,
        tone: "success",
      });
    } catch (error) {
      const message = (error as Error).message;
      setErrorMessage(`Failed to sync Jira work: ${message}`);
      showToast({
        title: "Jira sync failed",
        description: message,
        tone: "error",
      });
    } finally {
      setSyncing(false);
    }
  }

  const parentWorkflows = useMemo(
    () => allWorkflows.filter(isParentPlanningWorkflow),
    [allWorkflows],
  );
  const latestReconciliationWorkflow = useMemo(
    () => allWorkflows.filter(isJiraProjectReconciliationWorkflow).sort((left, right) => latestWorkflowActivity(right).localeCompare(latestWorkflowActivity(left)))[0] ?? null,
    [allWorkflows],
  );
  const cards = useMemo(() => parentWorkflows.map(buildParentWorkCard), [parentWorkflows]);
  const cardsByLane = useMemo(() => {
    return BOARD_LANES.reduce<Record<BoardLaneKey, ParentWorkCard[]>>(
      (accumulator, lane) => {
        accumulator[lane.key] = cards.filter((card) => card.lane === lane.key);
        return accumulator;
      },
      { needs_input: [], planning: [], ready: [], engineering: [], done: [] },
    );
  }, [cards]);

  return (
    <section className="space-y-4">
      {latestReconciliationWorkflow ? (
        <div className="flex flex-col gap-3 rounded-2xl border border-border bg-background px-4 py-4 md:flex-row md:items-center md:justify-between">
          <div className="space-y-1">
            <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">Latest Jira reconciliation</div>
            <div className="flex items-center gap-2 text-sm">
              <StatusBadge status={latestReconciliationWorkflow.status} />
              <span className="text-muted-foreground">
                Updated {formatTimeAgo(latestWorkflowActivity(latestReconciliationWorkflow))}
              </span>
            </div>
          </div>
          <Button asChild type="button" variant="outline" size="sm">
            <Link href={`/${encodeURIComponent(tenantId)}/executions/${encodeURIComponent(latestReconciliationWorkflow.execution_id)}`}>
              View execution
            </Link>
          </Button>
        </div>
      ) : null}

      {allowJiraReconciliation ? (
        <div className="flex justify-end">
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 w-8 p-0"
            onClick={() => void syncJiraWork()}
            disabled={!ready || !credentials || syncing}
            aria-label="Sync"
            title="Sync"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${syncing ? "animate-spin" : ""}`} />
          </Button>
        </div>
      ) : null}

      {errorMessage ? (
        <div className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {errorMessage}
        </div>
      ) : null}

      <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-5">
        {BOARD_LANES.map((lane) => (
          <BoardColumn
            key={lane.key}
            lane={lane}
            cards={cardsByLane[lane.key]}
            loading={loading}
            onOpenDetails={setSelectedCard}
          />
        ))}
      </div>

      <ParentWorkDetailsDrawer
        card={selectedCard}
        onClose={() => setSelectedCard(null)}
      />
    </section>
  );
}

function BoardColumn({
  lane,
  cards,
  loading,
  onOpenDetails,
}: {
  lane: BoardLane;
  cards: ParentWorkCard[];
  loading: boolean;
  onOpenDetails: (card: ParentWorkCard) => void;
}) {
  return (
    <div className="rounded-2xl border bg-muted/20">
      <div className="border-b bg-background/70 p-4">
        <h3 className="truncate text-sm font-semibold">{lane.title}</h3>
      </div>
      <div className="space-y-3 p-3">
        {loading ? (
          <>
            <Skeleton className="h-32 w-full rounded-xl" />
            <Skeleton className="h-28 w-full rounded-xl" />
          </>
        ) : cards.length === 0 ? (
          <p className="rounded-xl border border-dashed bg-background/60 px-3 py-8 text-center text-xs text-muted-foreground">
            No parent work here.
          </p>
        ) : (
          cards.map((card) => (
            <ParentWorkItemCard
              key={card.workflow.execution_id}
              card={card}
              onOpenDetails={onOpenDetails}
            />
          ))
        )}
      </div>
    </div>
  );
}

function ParentWorkItemCard({
  card,
  onOpenDetails,
}: {
  card: ParentWorkCard;
  onOpenDetails: (card: ParentWorkCard) => void;
}) {
  return (
    <article
      className="cursor-pointer rounded-xl border bg-background p-4 shadow-sm transition hover:-translate-y-0.5 hover:shadow-md focus-within:ring-2 focus-within:ring-primary/30"
      onClick={() => onOpenDetails(card)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpenDetails(card);
        }
      }}
      role="button"
      tabIndex={0}
      aria-label={`Open ${card.issueKey} details`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs font-medium text-primary">{card.issueKey}</p>
          <h4 className="mt-1 line-clamp-2 text-sm font-semibold leading-5">{card.title}</h4>
        </div>
      </div>

      <div className="mt-4 flex items-center justify-between gap-3 border-t pt-3">
        <p className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <Clock3 className="h-3.5 w-3.5" />
          {formatTimeAgo(latestWorkflowActivity(card.workflow))}
        </p>
      </div>
    </article>
  );
}

function ParentWorkDetailsDrawer({
  card,
  onClose,
}: {
  card: ParentWorkCard | null;
  onClose: () => void;
}) {
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  if (!card || !mounted) return null;

  return createPortal(
    <div className="fixed inset-0 z-[80] flex justify-end bg-slate-950/35" role="dialog" aria-modal="true" aria-label={`${card.issueKey} details`}>
      <button type="button" className="absolute inset-0 cursor-default" aria-label="Close details" onClick={onClose} />
      <aside className="relative flex h-full w-full max-w-md flex-col border-l bg-background shadow-2xl">
        <header className="border-b px-5 py-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <p className="text-xs font-medium text-primary">{card.issueKey}</p>
              <h2 className="mt-1 line-clamp-2 text-lg font-semibold leading-6">{card.title}</h2>
            </div>
            <Button variant="ghost" size="sm" className="h-8 w-8 p-0" onClick={onClose} aria-label="Close details">
              <X className="h-4 w-4" />
            </Button>
          </div>
          <p className="mt-4 inline-flex items-center gap-1.5 text-xs text-muted-foreground">
            <Clock3 className="h-3.5 w-3.5" />
            {formatTimeAgo(latestWorkflowActivity(card.workflow))}
          </p>
        </header>

        <div className="flex-1 space-y-6 overflow-y-auto px-5 py-5">
          <section>
            <h3 className="text-sm font-semibold">Work state</h3>
            <div className="mt-3 grid grid-cols-2 gap-4 rounded-xl border bg-muted/20 p-4">
              <DetailValue label="Runs" value={`${card.runCount} ${card.runCount === 1 ? "run" : "runs"}`} />
              <DetailValue label="Active" value={String(card.activeRunCount)} />
              <DetailValue label="Failed" value={String(card.failedRunCount)} />
              <DetailValue label="Questions" value={card.questionOpen ? "Open" : "None"} />
            </div>
          </section>

          <section>
            <h3 className="text-sm font-semibold">Latest engineering run</h3>
            {card.latestRun ? (
              <div className="mt-3 rounded-xl border p-4 text-sm">
                <div className="flex items-center justify-between gap-3">
                  <p className="truncate font-medium">{card.latestRun.issue_key || card.latestRun.run_id}</p>
                  <StatusBadge status={card.latestRun.status} />
                </div>
                <p className="mt-2 text-muted-foreground">{card.latestRun.issue_summary ?? "Engineering run"}</p>
              </div>
            ) : (
              <p className="mt-3 rounded-xl border border-dashed px-4 py-5 text-sm text-muted-foreground">
                No engineering runs have started for this parent item.
              </p>
            )}
          </section>
        </div>

      </aside>
    </div>,
    document.body,
  );
}

function DetailValue({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-0.5 font-semibold">{value}</p>
    </div>
  );
}
