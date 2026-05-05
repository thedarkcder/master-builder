"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, ExternalLink, MoreHorizontal, RefreshCw } from "lucide-react";

import { ExecutionObservabilityDrawer, type ExecutionObservabilityView } from "@/components/execution-observability-drawer";
import { WorkflowFlowDiagram } from "@/components/workflow-flow-diagram";
import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useToast } from "@/components/ui/toast-provider";
import {
  getWorkflow,
  getWorkflowOperationAttemptAudit,
  resumeWorkflowExecution,
  restartWorkflowOperation,
  retryWorkflowOperation,
  streamWorkflowOperationTelemetryEvents,
  type WorkflowObservabilityEventRecord,
  type WorkflowOperationAttemptRecord,
  type WorkflowOperationRecord,
  type WorkflowStepAttemptTranscriptRecord,
  type WorkflowRecord,
} from "@/lib/api";
import { formatTimeAgo, formatTimestamp } from "@/lib/datetime";
import { compareWorkflowObservabilityEvents } from "@/lib/workflow-event-order";
import { buildTelemetryAttemptView } from "@/lib/workflow-observability";
import { cn } from "@/lib/utils";

const LIVE_TELEMETRY_EVENT_LIMIT = 1000;

function latestAttempt(operation: WorkflowOperationRecord) {
  return [...operation.attempts].sort((left, right) => {
    const leftTime = new Date(left.finished_at ?? left.started_at ?? 0).getTime();
    const rightTime = new Date(right.finished_at ?? right.started_at ?? 0).getTime();
    return rightTime - leftTime;
  })[0] ?? null;
}

function sortAttempts(attempts: WorkflowOperationAttemptRecord[]): WorkflowOperationAttemptRecord[] {
  return [...attempts].sort((left, right) => right.attempt_number - left.attempt_number);
}

function mergeObservabilityEvents(
  current: WorkflowObservabilityEventRecord[],
  incoming: WorkflowObservabilityEventRecord[],
): WorkflowObservabilityEventRecord[] {
  const byId = new Map<string, WorkflowObservabilityEventRecord>();
  for (const event of [...current, ...incoming]) {
    byId.set(event.event_id, event);
  }
  return [...byId.values()].sort(compareWorkflowObservabilityEvents).slice(-LIVE_TELEMETRY_EVENT_LIMIT);
}

function attemptFailure(attempt: ReturnType<typeof latestAttempt>) {
  if (!attempt) return null;
  return ["failed", "retrying"].includes(String(attempt.status || "").trim().toLowerCase())
    ? (attempt.error_message?.trim() || null)
    : null;
}

function attemptStatusDetail(attempt: ReturnType<typeof latestAttempt>) {
  if (!attempt) return null;
  return attempt.status_detail?.trim() || null;
}

function impactLabel(required?: boolean): string {
  return required === false ? "Supporting" : "Must finish";
}

function isSupportingOperation(operation: WorkflowOperationRecord): boolean {
  return operation.required === false;
}

function latestAttemptCategory(attempt: ReturnType<typeof latestAttempt>) {
  return attempt?.error_category?.trim().toLowerCase() || null;
}

function isAttemptLifecycleEvent(event: WorkflowObservabilityEventRecord): boolean {
  return [
    "workflow_operation_attempt_started",
    "workflow_operation_attempt_completed",
    "workflow_operation_attempt_failed",
    "workflow_operation_attempt_waiting_for_input",
    "workflow_operation_waiting_for_input",
    "workflow_operation_attempt_retried",
  ].includes(String(event.event_kind || "").trim().toLowerCase());
}

function attemptStatusFromLifecycleEvent(event: WorkflowObservabilityEventRecord): WorkflowOperationAttemptRecord["status"] | null {
  const kind = String(event.event_kind || "").trim().toLowerCase();
  if (kind === "workflow_operation_attempt_started" || kind === "workflow_operation_attempt_retried") return "running";
  if (kind === "workflow_operation_attempt_completed") return "completed";
  if (kind === "workflow_operation_attempt_failed") return "failed";
  if (kind === "workflow_operation_attempt_waiting_for_input" || kind === "workflow_operation_waiting_for_input") return "waiting_for_input";
  return null;
}

function isTerminalAttemptStatus(status: string | null | undefined): boolean {
  return ["completed", "failed", "waiting_for_input"].includes(String(status || "").trim().toLowerCase());
}

function mergeAttemptLifecycleEventIntoWorkflow(
  workflow: WorkflowRecord,
  operationId: string,
  event: WorkflowObservabilityEventRecord,
): WorkflowRecord {
  if (!event.attempt_id || !isAttemptLifecycleEvent(event)) {
    return workflow;
  }
  const eventAttemptId = event.attempt_id;
  const nextStatus = attemptStatusFromLifecycleEvent(event);
  if (!nextStatus) {
    return workflow;
  }
  return {
    ...workflow,
    operations: workflow.operations.map((operation) => {
      if (operation.operation_id !== operationId) {
        return operation;
      }
      const payload = event.payload ?? {};
      const attemptNumber =
        typeof payload.attempt_number === "number"
          ? payload.attempt_number
          : typeof event.attempt === "number"
            ? event.attempt
            : Math.max(1, ...operation.attempts.map((attempt) => attempt.attempt_number));
      const existingAttempt = operation.attempts.find((attempt) => attempt.attempt_id === eventAttemptId);
      if (!existingAttempt) {
        return operation;
      }
      if (existingAttempt.finished_at && new Date(event.recorded_at).getTime() <= new Date(existingAttempt.finished_at).getTime()) {
        return operation;
      }
      if (isTerminalAttemptStatus(existingAttempt.status) && nextStatus === "running") {
        return operation;
      }
      const nextAttempt: WorkflowOperationAttemptRecord = {
        attempt_id: eventAttemptId,
        attempt_number: existingAttempt.attempt_number ?? attemptNumber,
        status: nextStatus,
        error_category:
          nextStatus === "failed" ? String(payload.error_category || existingAttempt.error_category || "").trim() || null : null,
        error_message: nextStatus === "failed" ? event.message || existingAttempt.error_message || null : null,
        status_detail: event.message || existingAttempt.status_detail || null,
        retryable: existingAttempt.retryable ?? false,
        next_retry_at: existingAttempt.next_retry_at ?? null,
        started_at: existingAttempt.started_at ?? event.recorded_at,
        finished_at: ["completed", "failed", "waiting_for_input"].includes(nextStatus)
          ? event.recorded_at
          : existingAttempt.finished_at ?? null,
        work_units: existingAttempt.work_units ?? [],
      };
      const attempts = operation.attempts.map((attempt) => (attempt.attempt_id === eventAttemptId ? nextAttempt : attempt));
      return {
        ...operation,
        status: nextStatus,
        attempts,
        summary: event.message || operation.summary,
        can_retry: nextStatus === "failed" ? operation.can_retry : false,
        can_restart: false,
      };
    }),
  };
}

export default function TenantExecutionDetailPage() {
  const params = useParams<{ tenantId: string; workflowId: string }>();
  const router = useRouter();
  const { credentials, ready } = useAuth();
  const { showToast } = useToast();
  const tenantId = decodeURIComponent(params.tenantId);
  const executionId = decodeURIComponent(params.workflowId);

  const [workflow, setWorkflow] = useState<WorkflowRecord | null>(null);
  const [loading, setLoading] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [retryingOperationId, setRetryingOperationId] = useState<string | null>(null);
  const [restartingOperationId, setRestartingOperationId] = useState<string | null>(null);
  const [statusLine, setStatusLine] = useState("");
  const [activeTab, setActiveTab] = useState<"overview" | "step-recovery" | "execution-path">("overview");
  const [linksMenuOpen, setLinksMenuOpen] = useState(false);
  const [selectedOperationId, setSelectedOperationId] = useState<string | null>(null);
  const [selectedAttemptId, setSelectedAttemptId] = useState<string | null>(null);
  const [attemptSelectionMode, setAttemptSelectionMode] = useState<"follow_latest" | "pinned">("follow_latest");
  const [observabilityView, setObservabilityView] = useState<ExecutionObservabilityView>("telemetry");
  const [telemetryEvents, setTelemetryEvents] = useState<WorkflowObservabilityEventRecord[]>([]);
  const [auditAttempt, setAuditAttempt] = useState<WorkflowStepAttemptTranscriptRecord | null>(null);
  const [telemetryLoading, setTelemetryLoading] = useState(false);
  const [auditLoading, setAuditLoading] = useState(false);
  const [observabilityError, setObservabilityError] = useState<string | null>(null);
  const [telemetryRefreshNonce, setTelemetryRefreshNonce] = useState(0);
  const [auditRefreshNonce, setAuditRefreshNonce] = useState(0);
  const [awaitingNewAttemptForOperationId, setAwaitingNewAttemptForOperationId] = useState<string | null>(null);
  const attemptSelectionModeRef = useRef(attemptSelectionMode);
  const awaitingNewAttemptForOperationIdRef = useRef(awaitingNewAttemptForOperationId);
  const workflowRef = useRef<WorkflowRecord | null>(workflow);
  const workflowRefreshesByAttemptRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    attemptSelectionModeRef.current = attemptSelectionMode;
  }, [attemptSelectionMode]);

  useEffect(() => {
    awaitingNewAttemptForOperationIdRef.current = awaitingNewAttemptForOperationId;
  }, [awaitingNewAttemptForOperationId]);

  useEffect(() => {
    workflowRef.current = workflow;
  }, [workflow]);

  const loadWorkflow = useCallback(async () => {
    if (!credentials) return;
    setLoading(true);
    try {
      const payload = await getWorkflow(credentials, executionId);
      setWorkflow(payload);
      setStatusLine("");
      return payload;
    } catch (error) {
      setStatusLine(`Failed to load execution: ${(error as Error).message}`);
      setWorkflow(null);
      return null;
    } finally {
      setLoading(false);
    }
  }, [credentials, executionId]);

  useEffect(() => {
    if (ready && credentials) void loadWorkflow();
  }, [ready, credentials, loadWorkflow]);

  const hasRetryableAttempt = useMemo(() => (workflow?.operations ?? []).some((operation) => operation.can_retry), [workflow?.operations]);
  const jiraIssueLink = useMemo(
    () => workflow?.links.find((link) => link.kind === "jira_issue" && link.url)?.url ?? null,
    [workflow?.links],
  );
  const selectedOperation = useMemo(
    () => workflow?.operations.find((operation) => operation.operation_id === selectedOperationId) ?? null,
    [selectedOperationId, workflow?.operations],
  );
  const selectedTelemetryOperationId = selectedOperation?.operation_id ?? null;
  const selectedOperationAttempts = useMemo(
    () => (selectedOperation ? sortAttempts(selectedOperation.attempts) : []),
    [selectedOperation],
  );
  const selectedAttempt = useMemo(
    () => selectedOperationAttempts.find((attempt) => attempt.attempt_id === selectedAttemptId) ?? null,
    [selectedAttemptId, selectedOperationAttempts],
  );
  const telemetryAttemptView = useMemo(
    () => (selectedAttempt ? buildTelemetryAttemptView(selectedAttempt, telemetryEvents) : null),
    [selectedAttempt, telemetryEvents],
  );
  const activeOperations = useMemo(
    () => (workflow?.operations ?? []).filter((operation) => ["running", "retrying", "waiting_for_input"].includes(operation.status)),
    [workflow?.operations],
  );
  const activeBlockingOperations = useMemo(() => {
    const activeOperationTypes = new Set(activeOperations.map((operation) => operation.operation_type));
    return activeOperations.filter((operation) => !operation.after.some((dependency) => activeOperationTypes.has(dependency)));
  }, [activeOperations]);
  const currentWorkSummary = useMemo(() => {
    if (!workflow) return "—";
    const running = activeBlockingOperations.filter((operation) => ["running", "retrying"].includes(operation.status));
    if (running.length > 0) {
      return `Running ${running.map((operation) => operation.label?.trim() || operation.operation_type).join(", ")}`;
    }
    const waiting = activeBlockingOperations.filter((operation) => operation.status === "waiting_for_input");
    if (waiting.length > 0) {
      return `Waiting on ${waiting.map((operation) => operation.label?.trim() || operation.operation_type).join(", ")}`;
    }
    return workflow.failure_reason?.trim() || workflow.next_step?.trim() || "No active operation.";
  }, [activeBlockingOperations, workflow]);
  const flowNodes = useMemo(
    () =>
      (workflow?.operations ?? []).map((operation) => {
        const attempt = latestAttempt(operation);
        return {
          key: operation.operation_id,
          operationType: operation.operation_type,
          label: operation.label?.trim() || operation.operation_type,
          status: operation.status,
          impact: isSupportingOperation(operation) ? "Supporting" : impactLabel(operation.required),
          completionRequired: !isSupportingOperation(operation),
          after: operation.after,
          supports: operation.supports,
          detail: attemptFailure(attempt) || (operation.status === "waiting_for_input" ? attemptStatusDetail(attempt) : null),
        };
      }),
    [workflow?.operations],
  );

  const openOperationDrawer = useCallback((operation: WorkflowOperationRecord) => {
    setSelectedOperationId(operation.operation_id);
    setAttemptSelectionMode("follow_latest");
    setSelectedAttemptId(latestAttempt(operation)?.attempt_id ?? null);
    setAwaitingNewAttemptForOperationId(null);
    setObservabilityView("telemetry");
    setTelemetryEvents([]);
    setAuditAttempt(null);
    setObservabilityError(null);
    setTelemetryRefreshNonce((value) => value + 1);
    setAuditRefreshNonce((value) => value + 1);
  }, []);

  useEffect(() => {
    if (!selectedOperation) {
      setSelectedAttemptId(null);
      return;
    }
    if (attemptSelectionMode === "follow_latest") {
      if (awaitingNewAttemptForOperationId === selectedOperation.operation_id && selectedOperationAttempts.length === 0) {
        setSelectedAttemptId(null);
        return;
      }
      setSelectedAttemptId(selectedOperationAttempts[0]?.attempt_id ?? null);
      return;
    }
    if (selectedAttemptId && selectedOperationAttempts.some((attempt) => attempt.attempt_id === selectedAttemptId)) {
      return;
    }
    if (awaitingNewAttemptForOperationId === selectedOperation.operation_id) {
      setSelectedAttemptId(null);
      return;
    }
    setSelectedAttemptId(selectedOperationAttempts[0]?.attempt_id ?? null);
  }, [attemptSelectionMode, awaitingNewAttemptForOperationId, selectedAttemptId, selectedOperation, selectedOperationAttempts]);

  useEffect(() => {
    setTelemetryEvents([]);
    setAuditAttempt(null);
  }, [selectedOperationId]);

  useEffect(() => {
    setAuditAttempt(null);
  }, [selectedAttemptId]);

  useEffect(() => {
    const operationId = selectedTelemetryOperationId;
    if (!operationId || !credentials || !["telemetry", "metrics"].includes(observabilityView)) {
      return;
    }
    let disposed = false;
    const controller = new AbortController();
    setTelemetryLoading(true);
    setObservabilityError(null);

    const handleEvent = (event: WorkflowObservabilityEventRecord) => {
      if (disposed) {
        return;
      }
      setTelemetryLoading(false);
      setTelemetryEvents((current) => mergeObservabilityEvents(current, [event]));
      if (isAttemptLifecycleEvent(event) && event.attempt_id) {
        setWorkflow((current) => (current ? mergeAttemptLifecycleEventIntoWorkflow(current, operationId, event) : current));
      }
      if (event.event_kind === "workflow_operation_attempt_started" && event.attempt_id) {
        const startedAttemptId = event.attempt_id;
        const attemptKnown = workflowRef.current?.operations
          .find((operation) => operation.operation_id === operationId)
          ?.attempts.some((attempt) => attempt.attempt_id === startedAttemptId) ?? false;
        if (!attemptKnown && !workflowRefreshesByAttemptRef.current.has(startedAttemptId)) {
          workflowRefreshesByAttemptRef.current.add(startedAttemptId);
          void loadWorkflow().finally(() => {
            workflowRefreshesByAttemptRef.current.delete(startedAttemptId);
          });
        }
      }
      if (attemptSelectionModeRef.current === "follow_latest" && isAttemptLifecycleEvent(event) && event.attempt_id) {
        if (event.event_kind === "workflow_operation_attempt_started") {
          setSelectedAttemptId(event.attempt_id);
        }
        setAwaitingNewAttemptForOperationId((current) => (current === operationId ? null : current));
        return;
      }
      if (
        awaitingNewAttemptForOperationIdRef.current === operationId &&
        event.event_kind === "workflow_operation_attempt_started" &&
        event.attempt_id
      ) {
        setSelectedAttemptId(event.attempt_id);
        setAwaitingNewAttemptForOperationId(null);
      }
    };

    void streamWorkflowOperationTelemetryEvents(credentials, executionId, operationId, handleEvent, {
      signal: controller.signal,
      onOpen: () => {
        if (!disposed) {
          setTelemetryLoading(false);
        }
      },
    }).catch((error) => {
        if (disposed || controller.signal.aborted) {
          return;
        }
        setObservabilityError(`Failed to stream live telemetry: ${(error as Error).message}`);
        setTelemetryLoading(false);
    });

    return () => {
      disposed = true;
      controller.abort();
    };
  }, [
    credentials,
    executionId,
    observabilityView,
    selectedTelemetryOperationId,
    telemetryRefreshNonce,
    loadWorkflow,
  ]);

  useEffect(() => {
    if (!selectedOperation || !selectedAttemptId || !credentials || !workflow || observabilityView !== "audit") {
      return;
    }
    let disposed = false;
    setAuditLoading(true);
    setObservabilityError(null);
    void getWorkflowOperationAttemptAudit(
      credentials,
      workflow.execution_id,
      selectedOperation.operation_id,
      selectedAttemptId,
      { limit: 500 },
    )
      .then((attempt) => {
        if (!disposed) {
          setAuditAttempt(attempt);
        }
      })
      .catch((error) => {
        if (!disposed) {
          setObservabilityError(`Failed to load audit history: ${(error as Error).message}`);
        }
      })
      .finally(() => {
        if (!disposed) {
          setAuditLoading(false);
        }
      });
    return () => {
      disposed = true;
    };
  }, [auditRefreshNonce, credentials, observabilityView, selectedAttemptId, selectedOperation, workflow]);

  async function handleWorkflowAction() {
    if (!credentials || !workflow) {
      return;
    }
    if (!workflow.can_resume) {
      setStatusLine(workflow.resume_unavailable_reason?.trim() || "Execution cannot be resumed.");
      return;
    }
    setRetrying(true);
    try {
      const nextRun = await resumeWorkflowExecution(credentials, workflow.execution_id);
      showToast({ title: "Execution resume queued", description: `Run ${nextRun.run_id}`, tone: "success" });
      router.push(`/${encodeURIComponent(tenantId)}/runs/${encodeURIComponent(nextRun.run_id)}`);
    } catch (error) {
      showToast({ title: "Execution resume failed", description: (error as Error).message, tone: "error" });
    } finally {
      setRetrying(false);
    }
  }

  async function handleRetryOperation(operation: WorkflowOperationRecord) {
    if (!credentials || !workflow || operation.definition_only) {
      return;
    }
    setSelectedOperationId(operation.operation_id);
    setAttemptSelectionMode("follow_latest");
    setSelectedAttemptId(null);
    setAwaitingNewAttemptForOperationId(operation.operation_id);
    setObservabilityView("telemetry");
    setTelemetryEvents([]);
    setAuditAttempt(null);
    setObservabilityError(null);
    setTelemetryLoading(true);
    setAuditLoading(false);
    setTelemetryRefreshNonce((value) => value + 1);
    setRetryingOperationId(operation.operation_id);
    try {
      const retryResult = await retryWorkflowOperation(credentials, workflow.execution_id, operation.operation_id);
      setWorkflow(retryResult.workflow);
      if (retryResult.started_attempt?.attempt_id) {
        setSelectedAttemptId(retryResult.started_attempt.attempt_id);
        setAwaitingNewAttemptForOperationId(null);
      }
      showToast({ title: "Operation retry queued", description: operation.label?.trim() || operation.operation_type, tone: "success" });
    } catch (error) {
      setTelemetryLoading(false);
      setAwaitingNewAttemptForOperationId(null);
      showToast({ title: "Operation retry failed", description: (error as Error).message, tone: "error" });
    } finally {
      setRetryingOperationId(null);
    }
  }

  async function handleRestartOperation(operation: WorkflowOperationRecord) {
    if (!credentials || !workflow || operation.definition_only) {
      return;
    }
    setSelectedOperationId(operation.operation_id);
    setAttemptSelectionMode("follow_latest");
    setSelectedAttemptId(null);
    setAwaitingNewAttemptForOperationId(operation.operation_id);
    setObservabilityView("telemetry");
    setTelemetryEvents([]);
    setAuditAttempt(null);
    setObservabilityError(null);
    setTelemetryLoading(true);
    setAuditLoading(false);
    setTelemetryRefreshNonce((value) => value + 1);
    setRestartingOperationId(operation.operation_id);
    try {
      const restartResult = await restartWorkflowOperation(
        credentials,
        workflow.execution_id,
        operation.operation_id,
        "Admin restarted stale running workflow operation attempt."
      );
      setWorkflow(restartResult.workflow);
      if (restartResult.started_attempt?.attempt_id) {
        setSelectedAttemptId(restartResult.started_attempt.attempt_id);
        setAwaitingNewAttemptForOperationId(null);
      }
      showToast({ title: "Operation restart queued", description: operation.label?.trim() || operation.operation_type, tone: "success" });
    } catch (error) {
      setTelemetryLoading(false);
      setAwaitingNewAttemptForOperationId(null);
      showToast({ title: "Operation restart failed", description: (error as Error).message, tone: "error" });
    } finally {
      setRestartingOperationId(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <Button variant="ghost" size="sm" asChild className="h-8">
          <Link href={`/${encodeURIComponent(tenantId)}/executions`}>
            <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />
            Back to executions
          </Link>
        </Button>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" className="h-8" onClick={() => void loadWorkflow()} disabled={loading}>
            <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", loading && "animate-spin")} />
            Refresh
          </Button>
          {workflow?.links?.length ? (
            <div className="relative">
              <Button
                variant="outline"
                size="sm"
                className="h-8"
                onClick={() => setLinksMenuOpen((open) => !open)}
                aria-expanded={linksMenuOpen}
                aria-haspopup="menu"
              >
                <MoreHorizontal className="mr-1.5 h-3.5 w-3.5" />
                Links
              </Button>
              {linksMenuOpen ? (
                <div className="absolute right-0 z-20 mt-2 w-80 rounded-xl border bg-background p-2 shadow-lg" role="menu">
                  <div className="space-y-1">
                    {workflow.links.map((link) => {
                      const itemKey = `${link.kind}:${link.ref ?? link.url ?? link.label}`;
                      const openHref =
                        link.kind === "run" && link.ref
                          ? `/${encodeURIComponent(tenantId)}/runs/${encodeURIComponent(link.ref)}`
                          : link.url;
                      return (
                        <div key={itemKey} className="rounded-lg px-3 py-2 hover:bg-muted/50">
                          <div className="flex items-center justify-between gap-2">
                            <div className="min-w-0">
                              <p className="truncate text-sm font-medium">{link.label}</p>
                              <p className="truncate text-xs text-muted-foreground">{link.ref ?? link.kind.replace(/_/g, " ")}</p>
                            </div>
                            {link.status ? <StatusBadge status={link.status} /> : null}
                          </div>
                          {openHref ? (
                            <Link
                              className="mt-2 inline-flex items-center gap-1 text-xs text-primary hover:underline"
                              href={openHref}
                              onClick={() => setLinksMenuOpen(false)}
                            >
                              Open
                              <ExternalLink className="h-3 w-3" />
                            </Link>
                          ) : null}
                        </div>
                      );
                    })}
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}
          {workflow ? (
            <>
              <Button size="sm" className="h-8" onClick={() => void handleWorkflowAction()} disabled={retrying || !workflow.can_resume}>
                <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", retrying && "animate-spin")} />
                Resume execution
              </Button>
            </>
          ) : null}
        </div>
      </div>

      {statusLine ? (
        <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p>
      ) : null}

      {workflow ? (
        <>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Execution</p>
              <p className="mt-2 text-sm font-semibold">{workflow.display_name?.trim() || workflow.source_ref}</p>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Workflow type</p>
              <Link
                className="mt-2 inline-flex text-sm font-semibold text-primary hover:underline"
                href={`/${encodeURIComponent(tenantId)}/workflows/${encodeURIComponent(workflow.workflow_type.key)}`}
              >
                {workflow.workflow_type.label}
              </Link>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Current state</p>
              <div className="mt-2">
                <StatusBadge status={workflow.current_state} />
              </div>
              <p className="mt-2 text-xs text-muted-foreground">
                {currentWorkSummary}
              </p>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Next action</p>
              <p className="mt-2 text-sm font-semibold">{workflow.next_step?.trim() || (workflow.can_resume ? "Resume execution" : "—")}</p>
              <p className="mt-1 text-xs text-muted-foreground">
                {workflow.can_resume
                  ? "Reuse this execution and rerun from the latest resumable state."
                  : workflow.resume_unavailable_reason?.trim() || `${(workflow.operations ?? []).filter((operation) => operation.status === "failed").length} failed operation${(workflow.operations ?? []).filter((operation) => operation.status === "failed").length === 1 ? "" : "s"}`}
              </p>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Created</p>
              <p className="mt-2 text-sm font-semibold">{formatTimestamp(workflow.created_at)}</p>
              <p className="mt-1 text-xs text-muted-foreground">Source: {workflow.source_system}:{workflow.source_ref}</p>
            </div>
          </div>

          <div className="space-y-4">
            <div className="overflow-x-auto border-b">
              <nav className="-mb-px flex min-w-max gap-0" aria-label="Execution detail tabs">
                {[
                  { key: "overview", label: "Overview" },
                  { key: "step-recovery", label: "Step details" },
                  { key: "execution-path", label: "Execution path" },
                ].map((tab) => {
                  const selected = activeTab === tab.key;
                  return (
                    <button
                      key={tab.key}
                      type="button"
                      onClick={() => setActiveTab(tab.key as "overview" | "step-recovery" | "execution-path")}
                      className={cn(
                        "inline-flex items-center border-b-2 px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors",
                        selected
                          ? "border-primary text-foreground"
                          : "border-transparent text-muted-foreground hover:border-border hover:text-foreground",
                      )}
                    >
                      {tab.label}
                    </button>
                  );
                })}
              </nav>
            </div>

            {activeTab === "overview" ? (
              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="border-b px-5 py-3">
                  <h2 className="text-sm font-semibold">Execution</h2>
                </div>
                <div className="grid gap-4 px-5 py-4 text-sm md:grid-cols-2 xl:grid-cols-4">
                  <div>
                    <p className="font-medium">State</p>
                    <p className="text-muted-foreground">{workflow.current_state}</p>
                  </div>
                  <div>
                    <p className="font-medium">Current work</p>
                    <p className="text-muted-foreground">{currentWorkSummary}</p>
                  </div>
                  <div>
                    <p className="font-medium">Failure</p>
                    <p className="text-muted-foreground">{workflow.failure_reason?.trim() || "—"}</p>
                  </div>
                  <div>
                    <p className="font-medium">Available recovery</p>
                    <p className="text-muted-foreground">
                      {hasRetryableAttempt
                        ? "Retry failed step"
                        : workflow.can_resume
                          ? "Resume execution"
                          : "—"}
                    </p>
                  </div>
                </div>
              </div>
            ) : activeTab === "step-recovery" ? (
              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="border-b px-5 py-3">
                  <h2 className="text-sm font-semibold">Step details</h2>
                </div>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Step</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Impact</TableHead>
                      <TableHead>Latest attempt</TableHead>
                      <TableHead>Failure</TableHead>
                      <TableHead className="text-right">Action</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {workflow.operations.map((operation) => {
                      const attempt = latestAttempt(operation);
                      const latestCategory = latestAttemptCategory(attempt);
                      const needsClarification = latestCategory === "missing_input" && Boolean(jiraIssueLink);
                      return (
                        <TableRow key={operation.operation_id}>
                          <TableCell className="font-medium">
                            <button type="button" className="text-left hover:text-primary hover:underline" onClick={() => void openOperationDrawer(operation)}>
                              {operation.label?.trim() || operation.operation_type}
                            </button>
                          </TableCell>
                          <TableCell>
                            <StatusBadge status={operation.status} />
                          </TableCell>
                          <TableCell className="text-sm text-muted-foreground">
                            {isSupportingOperation(operation) ? "Supporting" : impactLabel(operation.required)}
                          </TableCell>
                          <TableCell className="text-sm text-muted-foreground">
                            {attempt ? (
                              <div className="space-y-1">
                                <p>Attempt {attempt.attempt_number}</p>
                                <p title={attempt.finished_at ? formatTimestamp(attempt.finished_at) : undefined}>
                                  {attempt.finished_at ? formatTimeAgo(attempt.finished_at) : attempt.started_at ? "In progress" : "Waiting to start"}
                                </p>
                              </div>
                            ) : (
                              operation.status === "pending" ? "Not started" : "—"
                            )}
                          </TableCell>
                          <TableCell className="max-w-[440px] text-sm text-muted-foreground whitespace-pre-wrap">
                            {attemptFailure(attempt) || "—"}
                          </TableCell>
                          <TableCell className="text-right">
                            <div className="flex flex-col items-end gap-2">
                              <Button
                                size="sm"
                                variant="outline"
                                className="h-8"
                                onClick={() => void handleRetryOperation(operation)}
                                disabled={!operation.can_retry || operation.definition_only || retryingOperationId === operation.operation_id}
                              >
                                <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", retryingOperationId === operation.operation_id && "animate-spin")} />
                                Retry step
                              </Button>
                              {operation.can_restart ? (
                                <Button
                                  size="sm"
                                  variant="outline"
                                  className="h-8"
                                  onClick={() => void handleRestartOperation(operation)}
                                  disabled={operation.definition_only || restartingOperationId === operation.operation_id}
                                >
                                  <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", restartingOperationId === operation.operation_id && "animate-spin")} />
                                  Restart step
                                </Button>
                              ) : null}
                              {needsClarification && jiraIssueLink ? (
                                <Button size="sm" variant="ghost" className="h-8 px-2 text-muted-foreground" asChild>
                                  <a href={jiraIssueLink} target="_blank" rel="noreferrer">
                                    Open Jira issue
                                  </a>
                                </Button>
                              ) : null}
                            </div>
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            ) : (
              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="px-5 py-4">
                  <WorkflowFlowDiagram
                    nodes={flowNodes}
                    emptyLabel="No execution path has been recorded yet."
                    orientation="vertical"
                    onNodeClick={(node) => {
                      const operation = workflow.operations.find((candidate) => candidate.operation_id === node.key);
                      if (operation) {
                        void openOperationDrawer(operation);
                      }
                    }}
                  />
                </div>
              </div>
            )}
          </div>

          <ExecutionObservabilityDrawer
            open={selectedOperation !== null}
            operationLabel={selectedOperation?.label?.trim() || selectedOperation?.operation_type || "Step"}
            attempts={selectedOperationAttempts}
            selectedAttemptId={selectedAttemptId}
            onSelectAttemptId={(attemptId) => {
              setAttemptSelectionMode(attemptId ? "pinned" : "follow_latest");
              setSelectedAttemptId(attemptId);
            }}
            activeView={observabilityView}
            onViewChange={setObservabilityView}
            onRefresh={() => {
              if (observabilityView === "audit") {
                setAuditRefreshNonce((value) => value + 1);
              } else {
                setTelemetryRefreshNonce((value) => value + 1);
              }
            }}
            onClose={() => {
              setSelectedOperationId(null);
              setSelectedAttemptId(null);
              setAttemptSelectionMode("follow_latest");
              setAwaitingNewAttemptForOperationId(null);
              setTelemetryEvents([]);
              setAuditAttempt(null);
              setObservabilityError(null);
            }}
            currentStatus={selectedOperation?.status || "unknown"}
            telemetryAttempt={telemetryAttemptView}
            auditAttempt={auditAttempt}
            waitingForNewAttempt={
              Boolean(selectedOperation) &&
              awaitingNewAttemptForOperationId === selectedOperation?.operation_id &&
              !selectedAttemptId
            }
            telemetryLoading={telemetryLoading}
            auditLoading={auditLoading}
            error={observabilityError}
          />
        </>
      ) : (
        <div className="rounded-2xl border bg-background px-5 py-12 text-center text-sm text-muted-foreground">
          {loading ? "Loading execution…" : "Execution not found."}
        </div>
      )}
    </div>
  );
}
