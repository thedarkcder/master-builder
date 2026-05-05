"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { AlertTriangle, Clock3, FolderKanban } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import {
  isParentPlanningWorkflow,
  latestWorkflowActivity,
  workflowLane,
} from "@/components/project-parent-work-board";
import { Skeleton } from "@/components/ui/skeleton";
import {
  listProjects,
  listWorkflows,
  type ProjectRecord,
  type RunRecord,
  type WorkflowRecord,
} from "@/lib/api";
import { buildProjectSectionPath } from "@/lib/dashboard-paths";
import { formatTimeAgo } from "@/lib/datetime";

type WorkspaceDashboardParams = {
  tenantId?: string | string[];
};

type ProjectWorkSummary = {
  project: ProjectRecord;
  parentCount: number;
  planningReadyCount: number;
  engineeringStartedCount: number;
  needsInputCount: number;
  readyCount: number;
  queuedRunCount: number;
  runningRunCount: number;
  failedRunCount: number;
  completedRunCount: number;
  completedRunTodayCount: number;
  capacitySlots: number;
  activeRunMsToday: number;
  utilizationRate: number;
  longestWaitingMs: number;
  latestActivity: string | null;
};

type CapacityBucket = {
  label: string;
  utilizationRate: number;
};

type PipelineStage = {
  label: string;
  value: number;
  tone: "primary" | "muted" | "warning" | "danger";
};

type ConstraintMetric = {
  label: string;
  value: number;
  detail: string;
  tone: "primary" | "muted" | "warning" | "danger";
};

type WorkingDayWindow = {
  startMs: number;
  endMs: number;
  elapsedMs: number;
  totalMs: number;
  label: string;
};

const WORKING_DAY_START_HOUR = 9;
const WORKING_DAY_END_HOUR = 17;
const CAPACITY_BUCKETS = WORKING_DAY_END_HOUR - WORKING_DAY_START_HOUR;

function paramValue(value: string | string[] | undefined): string {
  return Array.isArray(value) ? value[0] ?? "" : value ?? "";
}

function timestampMs(value: string | null | undefined): number | null {
  if (!value) {
    return null;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function workingDayWindow(now = new Date()): WorkingDayWindow {
  const start = new Date(now);
  start.setHours(WORKING_DAY_START_HOUR, 0, 0, 0);
  const end = new Date(now);
  end.setHours(WORKING_DAY_END_HOUR, 0, 0, 0);
  const startMs = start.getTime();
  const endMs = end.getTime();
  return {
    startMs,
    endMs,
    elapsedMs: Math.max(0, Math.min(now.getTime(), endMs) - startMs),
    totalMs: Math.max(0, endMs - startMs),
    label: `${String(WORKING_DAY_START_HOUR).padStart(2, "0")}:00-${String(WORKING_DAY_END_HOUR).padStart(2, "0")}:00`,
  };
}

function overlapMs(startMs: number | null, endMs: number | null, startBoundaryMs: number, endBoundaryMs: number): number {
  if (startMs === null) {
    return 0;
  }
  const boundedEnd = endMs ?? Date.now();
  return Math.max(0, Math.min(boundedEnd, endBoundaryMs) - Math.max(startMs, startBoundaryMs));
}

function runActiveMsInWindow(run: RunRecord, window: WorkingDayWindow): number {
  if (run.status === "queued" || run.status === "cancelled") {
    return 0;
  }
  return overlapMs(timestampMs(run.started_at), timestampMs(run.finished_at), window.startMs, window.endMs);
}

function workflowCurrentWaitingMs(workflow: WorkflowRecord): number {
  if (workflowLane(workflow) !== "needs_input") {
    return 0;
  }
  const startedWaitingAt = timestampMs(latestWorkflowActivity(workflow)) ?? timestampMs(workflow.started_at) ?? timestampMs(workflow.created_at);
  return startedWaitingAt ? Math.max(0, Date.now() - startedWaitingAt) : 0;
}

function projectCapacitySlots(project: ProjectRecord): number {
  const configured = Number(project.effective_policy?.max_concurrent_runs ?? 1);
  if (!Number.isFinite(configured) || configured < 1) {
    return 1;
  }
  return Math.trunc(configured);
}

function utilizationPercent(activeMs: number, capacitySlots: number, workingDayElapsedMs: number): number {
  const capacityMs = Math.max(0, capacitySlots) * Math.max(0, workingDayElapsedMs);
  if (capacityMs <= 0) {
    return 0;
  }
  return Math.min(100, Math.round((activeMs / capacityMs) * 100));
}

function isSuccessfulRun(run: RunRecord): boolean {
  const status = String(run.status || "").trim().toLowerCase();
  return status === "succeeded" || status === "completed" || status === "done";
}

function isToday(value: string | null | undefined): boolean {
  if (!value) {
    return false;
  }
  return value.slice(0, 10) === new Date().toISOString().slice(0, 10);
}

function formatDuration(valueMs: number): string {
  if (!valueMs || valueMs <= 0) {
    return "0m";
  }
  const minutes = Math.round(valueMs / 60_000);
  if (minutes < 60) {
    return `${minutes}m`;
  }
  const hours = Math.round(minutes / 60);
  if (hours < 48) {
    return `${hours}h`;
  }
  return `${Math.round(hours / 24)}d`;
}

function formatSlotHours(valueMs: number): string {
  const hours = valueMs / 3_600_000;
  if (hours < 10) {
    return `${hours.toFixed(1)}h`;
  }
  return `${Math.round(hours)}h`;
}

function toneClass(tone: "primary" | "muted" | "warning" | "danger"): string {
  if (tone === "danger") {
    return "bg-destructive";
  }
  if (tone === "warning") {
    return "bg-warning";
  }
  if (tone === "muted") {
    return "bg-muted-foreground/35";
  }
  return "bg-primary";
}

function buildCapacityBuckets(runs: RunRecord[], capacitySlots: number, window: WorkingDayWindow): CapacityBucket[] {
  const bucketMs = window.totalMs / CAPACITY_BUCKETS;
  return Array.from({ length: CAPACITY_BUCKETS }, (_, index) => {
    const bucketStart = window.startMs + index * bucketMs;
    const bucketEnd = bucketStart + bucketMs;
    const activeMs = runs.reduce((total, run) => {
      if (run.status === "queued" || run.status === "cancelled") {
        return total;
      }
      return total + overlapMs(timestampMs(run.started_at), timestampMs(run.finished_at), bucketStart, bucketEnd);
    }, 0);
    return {
      label: `${String(WORKING_DAY_START_HOUR + index).padStart(2, "0")}:00`,
      utilizationRate: utilizationPercent(activeMs, capacitySlots, bucketMs),
    };
  });
}

export default function WorkspaceDashboardPage() {
  const params = useParams<WorkspaceDashboardParams>();
  const tenantId = paramValue(params.tenantId);
  const { credentials, ready } = useAuth();
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [workflows, setWorkflows] = useState<WorkflowRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    async function loadDashboard() {
      if (!ready || !credentials || !tenantId) return;
      setLoading(true);
      setErrorMessage(null);
      try {
        const [projectPayload, workflowPayload] = await Promise.all([
          listProjects(credentials, tenantId),
          listWorkflows(credentials, { tenantId, limit: 100 }),
        ]);
        if (disposed) return;
        setProjects(projectPayload);
        setWorkflows(workflowPayload.filter(isParentPlanningWorkflow));
      } catch (error) {
        if (!disposed) {
          setErrorMessage(`Failed to load dashboard: ${(error as Error).message}`);
        }
      } finally {
        if (!disposed) {
          setLoading(false);
        }
      }
    }
    void loadDashboard();
    return () => {
      disposed = true;
    };
  }, [credentials, ready, tenantId]);

  const activeProjects = useMemo(() => projects.filter((project) => !project.is_archived), [projects]);
  const workday = useMemo(() => workingDayWindow(), []);
  const allRuns = useMemo(() => workflows.flatMap((workflow) => workflow.runs), [workflows]);
  const projectSummaries = useMemo<ProjectWorkSummary[]>(() => {
    const workflowsByProject = new Map<string, WorkflowRecord[]>();
    for (const workflow of workflows) {
      if (!workflow.project_id) continue;
      const bucket = workflowsByProject.get(workflow.project_id) ?? [];
      bucket.push(workflow);
      workflowsByProject.set(workflow.project_id, bucket);
    }

    return activeProjects
      .map((project) => {
        const projectWorkflows = workflowsByProject.get(project.project_id) ?? [];
        const lanes = projectWorkflows.map(workflowLane);
        const projectRuns = projectWorkflows.flatMap((workflow) => workflow.runs);
        const capacitySlots = projectCapacitySlots(project);
        const activeRunMsToday = projectRuns.reduce((total, run) => total + runActiveMsInWindow(run, workday), 0);
        const latestActivity = projectWorkflows.length
          ? [...projectWorkflows.map(latestWorkflowActivity)].sort().at(-1) ?? null
          : null;

        return {
          project,
          parentCount: projectWorkflows.length,
          planningReadyCount: lanes.filter((lane) => lane === "ready" || lane === "engineering" || lane === "done").length,
          engineeringStartedCount: projectWorkflows.filter((workflow) => workflow.runs.length > 0).length,
          needsInputCount: lanes.filter((lane) => lane === "needs_input").length,
          readyCount: lanes.filter((lane) => lane === "ready").length,
          queuedRunCount: projectRuns.filter((run) => run.status === "queued").length,
          runningRunCount: projectRuns.filter((run) => run.status === "running").length,
          failedRunCount: projectRuns.filter((run) => run.status === "failed").length,
          completedRunCount: projectRuns.filter(isSuccessfulRun).length,
          completedRunTodayCount: projectRuns.filter((run) => isSuccessfulRun(run) && isToday(run.finished_at)).length,
          capacitySlots,
          activeRunMsToday,
          utilizationRate: utilizationPercent(activeRunMsToday, capacitySlots, workday.elapsedMs),
          longestWaitingMs: Math.max(0, ...projectWorkflows.map(workflowCurrentWaitingMs)),
          latestActivity,
        };
      })
      .sort((left, right) => {
        const leftPressure = left.readyCount * 86_400_000 + left.needsInputCount * 43_200_000 + left.longestWaitingMs;
        const rightPressure = right.readyCount * 86_400_000 + right.needsInputCount * 43_200_000 + right.longestWaitingMs;
        if (rightPressure !== leftPressure) {
          return rightPressure - leftPressure;
        }
        const leftActivity = left.latestActivity ?? left.project.updated_at;
        const rightActivity = right.latestActivity ?? right.project.updated_at;
        return rightActivity.localeCompare(leftActivity);
      });
  }, [activeProjects, workflows, workday]);

  const totalParents = projectSummaries.reduce((total, item) => total + item.parentCount, 0);
  const planningReady = projectSummaries.reduce((total, item) => total + item.planningReadyCount, 0);
  const engineeringStarted = projectSummaries.reduce((total, item) => total + item.engineeringStartedCount, 0);
  const completedRuns = projectSummaries.reduce((total, item) => total + item.completedRunCount, 0);
  const completedRunsToday = projectSummaries.reduce((total, item) => total + item.completedRunTodayCount, 0);
  const needsInput = projectSummaries.reduce((total, item) => total + item.needsInputCount, 0);
  const readyParents = projectSummaries.reduce((total, item) => total + item.readyCount, 0);
  const queuedRuns = projectSummaries.reduce((total, item) => total + item.queuedRunCount, 0);
  const runningRuns = projectSummaries.reduce((total, item) => total + item.runningRunCount, 0);
  const failedRuns = projectSummaries.reduce((total, item) => total + item.failedRunCount, 0);
  const capacitySlots = projectSummaries.reduce((total, item) => total + item.capacitySlots, 0);
  const activeRunMsToday = projectSummaries.reduce((total, item) => total + item.activeRunMsToday, 0);
  const utilizationRate = utilizationPercent(activeRunMsToday, capacitySlots, workday.elapsedMs);
  const capacityBuckets = useMemo(
    () => buildCapacityBuckets(allRuns, capacitySlots, workday),
    [allRuns, capacitySlots, workday],
  );
  const pipelineStages = useMemo<PipelineStage[]>(
    () => [
      { label: "Jira parent work", value: totalParents, tone: "muted" },
      { label: "Planning ready", value: planningReady, tone: "primary" },
      { label: "Engineering started", value: engineeringStarted, tone: "primary" },
      { label: "Runs completed", value: completedRuns, tone: "primary" },
      { label: "Needs input", value: needsInput, tone: needsInput > 0 ? "warning" : "muted" },
    ],
    [completedRuns, engineeringStarted, needsInput, planningReady, totalParents],
  );
  const constraints = useMemo<ConstraintMetric[]>(
    () => [
      {
        label: "Ready but idle",
        value: readyParents > 0 && queuedRuns + runningRuns === 0 ? readyParents : 0,
        detail: "Ready work exists but MB is not executing it.",
        tone: readyParents > 0 && queuedRuns + runningRuns === 0 ? "warning" : "muted",
      },
      {
        label: "Waiting for input",
        value: needsInput,
        detail: "Work needs product or stakeholder answers.",
        tone: needsInput > 0 ? "warning" : "muted",
      },
      {
        label: "Failed runs",
        value: failedRuns,
        detail: "Automation attempted work and hit a failure.",
        tone: failedRuns > 0 ? "danger" : "muted",
      },
      {
        label: "At capacity",
        value: runningRuns >= capacitySlots && capacitySlots > 0 ? runningRuns : 0,
        detail: "MB is saturated; extra work should queue.",
        tone: runningRuns >= capacitySlots && capacitySlots > 0 ? "primary" : "muted",
      },
    ],
    [capacitySlots, failedRuns, needsInput, queuedRuns, readyParents, runningRuns],
  );

  return (
    <main className="space-y-7">
      {errorMessage ? (
        <div className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {errorMessage}
        </div>
      ) : null}

      <section className="grid gap-4 md:grid-cols-4">
        <SignalTile loading={loading} label="Capacity used today" value={`${utilizationRate}%`} detail={`${formatSlotHours(activeRunMsToday)} active`} />
        <SignalTile loading={loading} label="Ready but idle" value={String(readyParents > 0 && queuedRuns + runningRuns === 0 ? readyParents : 0)} detail={`${readyParents} ready`} tone={readyParents > 0 && queuedRuns + runningRuns === 0 ? "warning" : "default"} />
        <SignalTile loading={loading} label="Completed today" value={String(completedRunsToday)} detail={`${completedRuns} total completed`} />
        <SignalTile loading={loading} label="Blocked now" value={String(needsInput + failedRuns)} detail={`${needsInput} waiting, ${failedRuns} failed`} tone={needsInput + failedRuns > 0 ? "warning" : "default"} />
      </section>

      <section className="grid gap-5 xl:grid-cols-[minmax(0,1.45fr)_minmax(340px,0.75fr)]">
        <CapacityPanel
          buckets={capacityBuckets}
          utilizationRate={utilizationRate}
          activeRunMsToday={activeRunMsToday}
          capacitySlots={capacitySlots}
          workday={workday}
          runningRuns={runningRuns}
          loading={loading}
        />
        <PickupPanel
          readyCount={readyParents}
          queuedCount={queuedRuns}
          runningCount={runningRuns}
          loading={loading}
        />
      </section>

      <section className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <ConversionPanel stages={pipelineStages} loading={loading} />
        <ConstraintPanel constraints={constraints} loading={loading} />
      </section>

      <ProjectConstraintPanel tenantId={tenantId} summaries={projectSummaries} loading={loading} />
    </main>
  );
}

function SignalTile({
  loading,
  label,
  value,
  detail,
  tone = "default",
}: {
  loading: boolean;
  label: string;
  value: string;
  detail: string;
  tone?: "default" | "warning";
}) {
  if (loading) {
    return <Skeleton className="h-28 rounded-[1.5rem]" />;
  }
  return (
    <div className={`rounded-[1.5rem] border bg-background p-5 ${tone === "warning" ? "border-warning/40 bg-warning/5" : ""}`}>
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="mt-3 text-4xl font-semibold tracking-tight">{value}</p>
      <p className="mt-2 text-sm text-muted-foreground">{detail}</p>
    </div>
  );
}

function CapacityPanel({
  buckets,
  utilizationRate,
  activeRunMsToday,
  capacitySlots,
  workday,
  runningRuns,
  loading,
}: {
  buckets: CapacityBucket[];
  utilizationRate: number;
  activeRunMsToday: number;
  capacitySlots: number;
  workday: WorkingDayWindow;
  runningRuns: number;
  loading: boolean;
}) {
  if (loading) {
    return <Skeleton className="h-[25rem] rounded-[2rem]" />;
  }
  const capacityMs = capacitySlots * workday.elapsedMs;
  return (
    <section className="rounded-[2rem] border bg-background p-6 shadow-sm">
      <div className="grid gap-6 lg:grid-cols-[minmax(220px,0.45fr)_minmax(0,1fr)] lg:items-end">
        <div>
          <p className="text-sm text-muted-foreground">Is MB working for us right now?</p>
          <p className="mt-4 text-6xl font-semibold tracking-tight">{utilizationRate}%</p>
          <p className="mt-3 text-sm text-muted-foreground">
            {formatSlotHours(activeRunMsToday)} used from {formatSlotHours(capacityMs)} available slot-hours.
          </p>
          <div className="mt-6 grid grid-cols-2 gap-3 text-sm">
            <MiniStat label="Running" value={String(runningRuns)} />
            <MiniStat label="Slots" value={String(capacitySlots)} />
            <MiniStat label="Window" value={workday.label} />
            <MiniStat label="Elapsed" value={formatDuration(workday.elapsedMs)} />
          </div>
        </div>
        <div>
          <div className="flex h-56 items-end gap-2">
            {buckets.map((bucket) => (
              <div key={bucket.label} className="flex min-w-0 flex-1 flex-col items-center gap-2">
                <div className="flex h-48 w-full items-end rounded-t-xl bg-muted/60">
                  <div
                    className="w-full rounded-t-xl bg-primary transition-all"
                    style={{ height: `${Math.max(2, bucket.utilizationRate)}%` }}
                    title={`${bucket.label}: ${bucket.utilizationRate}% utilized`}
                  />
                </div>
                <span className="text-[10px] text-muted-foreground">{bucket.label}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

function PickupPanel({
  readyCount,
  queuedCount,
  runningCount,
  loading,
}: {
  readyCount: number;
  queuedCount: number;
  runningCount: number;
  loading: boolean;
}) {
  if (loading) {
    return <Skeleton className="h-[25rem] rounded-[2rem]" />;
  }
  const inMotion = queuedCount + runningCount;
  const max = Math.max(1, readyCount, inMotion);
  const idleReady = readyCount > 0 && inMotion === 0;
  return (
    <section className={`rounded-[2rem] border bg-background p-6 shadow-sm ${idleReady ? "border-warning/40 bg-warning/5" : ""}`}>
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="text-sm text-muted-foreground">Are we leaving work on the table?</p>
          <h2 className="mt-3 text-2xl font-semibold">{idleReady ? "Ready work is idle" : "Ready work is being picked up"}</h2>
        </div>
        {idleReady ? <AlertTriangle className="h-5 w-5 text-warning" /> : null}
      </div>
      <div className="mt-9 space-y-7">
        <ComparisonBar label="Ready" value={readyCount} max={max} tone="muted" />
        <ComparisonBar label="In motion" value={inMotion} max={max} tone="primary" />
      </div>
      <div className="mt-8 grid grid-cols-2 gap-3">
        <MiniStat label="Queued" value={String(queuedCount)} />
        <MiniStat label="Running" value={String(runningCount)} />
      </div>
      <p className="mt-6 text-sm text-muted-foreground">
        {idleReady
          ? "There is planned work available, but MB has not started executing it."
          : "Ready work has active execution or queued capacity behind it."}
      </p>
    </section>
  );
}

function ConversionPanel({ stages, loading }: { stages: PipelineStage[]; loading: boolean }) {
  if (loading) {
    return <Skeleton className="h-96 rounded-[2rem]" />;
  }
  const max = Math.max(1, ...stages.map((stage) => stage.value));
  return (
    <section className="rounded-[2rem] border bg-background p-6 shadow-sm">
      <p className="text-sm text-muted-foreground">How much work did MB move forward?</p>
      <h2 className="mt-3 text-2xl font-semibold">Work conversion</h2>
      <div className="mt-8 space-y-5">
        {stages.map((stage) => (
          <div key={stage.label}>
            <div className="mb-2 flex items-center justify-between gap-3">
              <span className="text-sm text-muted-foreground">{stage.label}</span>
              <span className="text-sm font-medium">{stage.value}</span>
            </div>
            <div className="h-4 rounded-full bg-muted">
              <div
                className={`h-4 rounded-full ${toneClass(stage.tone)}`}
                style={{ width: `${stage.value > 0 ? Math.max(4, (stage.value / max) * 100) : 0}%` }}
              />
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function ConstraintPanel({ constraints, loading }: { constraints: ConstraintMetric[]; loading: boolean }) {
  if (loading) {
    return <Skeleton className="h-96 rounded-[2rem]" />;
  }
  const max = Math.max(1, ...constraints.map((constraint) => constraint.value));
  return (
    <section className="rounded-[2rem] border bg-background p-6 shadow-sm">
      <p className="text-sm text-muted-foreground">What is stopping delivery?</p>
      <h2 className="mt-3 text-2xl font-semibold">Current constraints</h2>
      <div className="mt-8 space-y-5">
        {constraints.map((constraint) => (
          <div key={constraint.label}>
            <div className="mb-2 flex items-center justify-between gap-3">
              <span className="text-sm font-medium">{constraint.label}</span>
              <span className="text-sm font-medium">{constraint.value}</span>
            </div>
            <div className="h-3 rounded-full bg-muted">
              <div
                className={`h-3 rounded-full ${toneClass(constraint.tone)}`}
                style={{ width: `${constraint.value > 0 ? Math.max(5, (constraint.value / max) * 100) : 0}%` }}
              />
            </div>
            <p className="mt-1 text-xs text-muted-foreground">{constraint.detail}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function ProjectConstraintPanel({
  tenantId,
  summaries,
  loading,
}: {
  tenantId: string;
  summaries: ProjectWorkSummary[];
  loading: boolean;
}) {
  if (loading) {
    return <Skeleton className="h-80 rounded-[2rem]" />;
  }
  const visibleSummaries = summaries
    .filter((summary) => summary.parentCount > 0 || summary.readyCount > 0 || summary.runningRunCount > 0 || summary.needsInputCount > 0)
    .slice(0, 6);

  return (
    <section className="rounded-[2rem] border bg-background shadow-sm">
      <div className="border-b p-6">
        <p className="text-sm text-muted-foreground">Where should we look next?</p>
        <h2 className="mt-3 text-2xl font-semibold">Project operating state</h2>
      </div>
      {visibleSummaries.length === 0 ? (
        <div className="p-6 text-sm text-muted-foreground">No active project work is currently tracked.</div>
      ) : (
        <div className="divide-y">
          {visibleSummaries.map((summary) => (
            <ProjectStateRow key={summary.project.project_id} tenantId={tenantId} summary={summary} />
          ))}
        </div>
      )}
    </section>
  );
}

function ProjectStateRow({ tenantId, summary }: { tenantId: string; summary: ProjectWorkSummary }) {
  const latest = summary.latestActivity ?? summary.project.updated_at;
  const inMotion = summary.queuedRunCount + summary.runningRunCount;
  const idleReady = summary.readyCount > 0 && inMotion === 0;
  return (
    <Link
      href={buildProjectSectionPath(tenantId, summary.project.project_id, "overview")}
      className="group grid gap-5 p-5 transition hover:bg-muted/40 focus:outline-none focus:ring-2 focus:ring-primary/30 md:grid-cols-[minmax(240px,0.8fr)_minmax(0,1.2fr)] md:items-center"
    >
      <div className="flex items-start gap-4">
        <div className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl ${idleReady ? "bg-warning/15 text-warning" : "bg-primary/10 text-primary"}`}>
          <FolderKanban className="h-5 w-5" />
        </div>
        <div className="min-w-0">
          <h3 className="truncate text-base font-semibold group-hover:text-primary">{summary.project.name}</h3>
          <p className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
            <Clock3 className="h-3.5 w-3.5" />
            Latest activity {formatTimeAgo(latest)}
          </p>
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-4">
        <MiniStat label="Capacity" value={`${summary.utilizationRate}%`} />
        <MiniStat label="Ready" value={String(summary.readyCount)} />
        <MiniStat label="Running" value={String(summary.runningRunCount)} />
        <MiniStat label="Blocked" value={String(summary.needsInputCount + summary.failedRunCount)} />
      </div>
    </Link>
  );
}

function ComparisonBar({
  label,
  value,
  max,
  tone,
}: {
  label: string;
  value: number;
  max: number;
  tone: "primary" | "muted";
}) {
  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="text-sm text-muted-foreground">{label}</span>
        <span className="text-sm font-medium">{value}</span>
      </div>
      <div className="h-5 rounded-full bg-muted">
        <div
          className={`h-5 rounded-full ${tone === "primary" ? "bg-primary" : "bg-muted-foreground/35"}`}
          style={{ width: `${value > 0 ? Math.max(4, (value / max) * 100) : 0}%` }}
        />
      </div>
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border bg-background/70 p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-sm font-semibold">{value}</p>
    </div>
  );
}
