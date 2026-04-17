"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, ExternalLink, RefreshCw } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { createWorkflowAttempt, getWorkflow, type WorkflowBlockerRecord, type WorkflowOperationRecord, type WorkflowRecord } from "@/lib/api";
import { formatTimestamp } from "@/lib/datetime";
import { cn } from "@/lib/utils";

function latestAttempt(operation: WorkflowOperationRecord) {
  return [...operation.attempts].sort((left, right) => {
    const leftTime = new Date(left.finished_at ?? left.started_at ?? 0).getTime();
    const rightTime = new Date(right.finished_at ?? right.started_at ?? 0).getTime();
    return rightTime - leftTime;
  })[0] ?? null;
}

function blockerForOperation(blockers: WorkflowBlockerRecord[], operation: WorkflowOperationRecord): WorkflowBlockerRecord | null {
  return blockers.find((blocker) => blocker.operation_id === operation.operation_id && blocker.status === "open") ?? null;
}

function joinItems(values: string[]): string {
  return values.length ? values.join(", ") : "—";
}

export default function TenantWorkflowDetailPage() {
  const params = useParams<{ tenantId: string; workflowId: string }>();
  const router = useRouter();
  const { credentials, ready } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);
  const workflowId = decodeURIComponent(params.workflowId);

  const [workflow, setWorkflow] = useState<WorkflowRecord | null>(null);
  const [loading, setLoading] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [statusLine, setStatusLine] = useState("");

  const loadWorkflow = useCallback(async () => {
    if (!credentials) return;
    setLoading(true);
    try {
      const payload = await getWorkflow(credentials, workflowId);
      setWorkflow(payload);
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to load execution: ${(error as Error).message}`);
      setWorkflow(null);
    } finally {
      setLoading(false);
    }
  }, [credentials, workflowId]);

  useEffect(() => {
    if (ready && credentials) void loadWorkflow();
  }, [ready, credentials, loadWorkflow]);

  const hasRetryableAttempt = useMemo(
    () => (workflow?.operations ?? []).some((operation) => operation.attempts.some((attempt) => attempt.retryable)),
    [workflow?.operations],
  );

  async function handleRetryFresh() {
    if (!credentials || !workflow) {
      return;
    }
    setRetrying(true);
    try {
      const nextRun = await createWorkflowAttempt(credentials, workflow.workflow_id, { mode: "fresh" });
      setStatusLine(`Queued retry as run ${nextRun.run_id}. Redirecting to run detail.`);
      router.push(`/${encodeURIComponent(tenantId)}/runs/${encodeURIComponent(nextRun.run_id)}`);
    } catch (error) {
      setStatusLine(`Failed to retry execution: ${(error as Error).message}`);
    } finally {
      setRetrying(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <Button variant="ghost" size="sm" asChild className="h-8">
          <Link href={`/${encodeURIComponent(tenantId)}/workflows`}>
            <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />
            Back to executions
          </Link>
        </Button>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" className="h-8" onClick={() => void loadWorkflow()} disabled={loading}>
            <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", loading && "animate-spin")} />
            Refresh
          </Button>
          <Button
            size="sm"
            className="h-8"
            onClick={() => void handleRetryFresh()}
            disabled={!workflow || retrying || !hasRetryableAttempt}
          >
            <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", retrying && "animate-spin")} />
            Retry execution
          </Button>
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
              <p className="mt-2 text-sm font-semibold">{workflow.issue_summary?.trim() || workflow.issue_key}</p>
              <p className="mt-1 text-xs text-muted-foreground">{workflow.workflow_id}</p>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Workflow type</p>
              <p className="mt-2 text-sm font-semibold">{workflow.workflow_type.label}</p>
              <p className="mt-1 text-xs text-muted-foreground">{workflow.workflow_type.key}</p>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Current state</p>
              <div className="mt-2">
                <StatusBadge status={workflow.current_state} />
              </div>
              <p className="mt-2 text-xs text-muted-foreground">
                {workflow.waiting_on ? `Waiting on ${workflow.waiting_on.replace(/_/g, " ")}.` : workflow.blocked_reason?.trim() || "No execution-level blocker recorded."}
              </p>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Next step</p>
              <p className="mt-2 text-sm font-semibold">{workflow.next_step?.trim() || "—"}</p>
              <p className="mt-1 text-xs text-muted-foreground">{workflow.blockers.length} blocker{workflow.blockers.length === 1 ? "" : "s"} open</p>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Created</p>
              <p className="mt-2 text-sm font-semibold">{formatTimestamp(workflow.created_at)}</p>
              <p className="mt-1 text-xs text-muted-foreground">Issue key: {workflow.issue_key}</p>
            </div>
          </div>

          <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <div className="overflow-hidden rounded-2xl border bg-background">
              <div className="border-b px-5 py-3">
                <h2 className="text-sm font-semibold">Execution path</h2>
              </div>
              <div className="space-y-3 px-5 py-4">
                {workflow.state_path.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No execution path has been recorded yet.</p>
                ) : (
                  workflow.state_path.map((entry) => (
                    <div key={entry.key} className="rounded-xl border bg-muted/20 p-4">
                      <div className="flex items-center justify-between gap-2">
                        <p className="text-sm font-medium">{entry.label}</p>
                        <StatusBadge status={entry.status} />
                      </div>
                      {entry.detail ? <p className="mt-2 text-sm text-muted-foreground">{entry.detail}</p> : null}
                      {entry.recorded_at ? (
                        <p className="mt-2 text-xs text-muted-foreground">{formatTimestamp(entry.recorded_at)}</p>
                      ) : null}
                    </div>
                  ))
                )}
              </div>
            </div>

            <div className="space-y-4">
              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="border-b px-5 py-3">
                  <h2 className="text-sm font-semibold">Step status</h2>
                </div>
                <div className="space-y-3 px-5 py-4 text-sm">
                  <div>
                    <p className="font-medium">Completed</p>
                    <p className="text-muted-foreground">{joinItems(workflow.completed_steps)}</p>
                  </div>
                  <div>
                    <p className="font-medium">Failed</p>
                    <p className="text-muted-foreground">{joinItems(workflow.failed_steps)}</p>
                  </div>
                  <div>
                    <p className="font-medium">Pending</p>
                    <p className="text-muted-foreground">{joinItems(workflow.pending_steps)}</p>
                  </div>
                  <div>
                    <p className="font-medium">Retrying</p>
                    <p className="text-muted-foreground">{joinItems(workflow.retrying_steps)}</p>
                  </div>
                </div>
              </div>

              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="border-b px-5 py-3">
                  <h2 className="text-sm font-semibold">Branches</h2>
                </div>
                <div className="space-y-3 px-5 py-4 text-sm">
                  <div>
                    <p className="font-medium">Taken</p>
                    <p className="text-muted-foreground">{joinItems(workflow.conditional_branches_taken)}</p>
                  </div>
                  <div>
                    <p className="font-medium">Available</p>
                    <p className="text-muted-foreground">{joinItems(workflow.conditional_branches_available)}</p>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div className="overflow-hidden rounded-2xl border bg-background">
            <div className="border-b px-5 py-3">
              <h2 className="text-sm font-semibold">Operations</h2>
            </div>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Operation</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Required</TableHead>
                  <TableHead>Latest attempt</TableHead>
                  <TableHead>Retryable</TableHead>
                  <TableHead>Failure / policy</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {workflow.operations.map((operation) => {
                  const attempt = latestAttempt(operation);
                  const blocker = blockerForOperation(workflow.blockers, operation);
                  return (
                    <TableRow key={operation.operation_id}>
                      <TableCell className="font-medium">
                        <p>{operation.label?.trim() || operation.operation_type}</p>
                        <p className="text-xs text-muted-foreground">{operation.operation_type}</p>
                        <p className="text-xs text-muted-foreground">
                          {operation.target_system ? `${operation.target_system}:${operation.target_ref ?? "—"}` : operation.definition_only ? "Defined in workflow type" : operation.operation_id}
                        </p>
                      </TableCell>
                      <TableCell>
                        <StatusBadge status={operation.status} />
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">{operation.required === false ? "Optional" : "Required"}</TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        {attempt ? (
                          <div className="space-y-1">
                            <p>Attempt {attempt.attempt_number}</p>
                            <p>{attempt.finished_at ? formatTimestamp(attempt.finished_at) : "In progress"}</p>
                          </div>
                        ) : (
                          "—"
                        )}
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        {attempt?.retryable ? "Yes" : "No"}
                      </TableCell>
                      <TableCell className="max-w-[440px] text-sm text-muted-foreground">
                        <div className="space-y-1">
                          <p className="line-clamp-3">
                            {blocker?.message?.trim() || attempt?.error_message?.trim() || operation.summary?.trim() || operation.description?.trim() || "—"}
                          </p>
                          <p className="text-xs text-muted-foreground">
                            {operation.retry_policy?.trim() || "No retry policy recorded."}
                          </p>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>

          <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <div className="overflow-hidden rounded-2xl border bg-background">
              <div className="border-b px-5 py-3">
                <h2 className="text-sm font-semibold">Blockers</h2>
              </div>
              <div className="space-y-3 px-5 py-4">
                {workflow.blockers.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No blockers recorded.</p>
                ) : (
                  workflow.blockers.map((blocker) => (
                    <div key={blocker.blocker_id} className="rounded-xl border bg-muted/20 p-4">
                      <div className="flex items-center gap-2">
                        <StatusBadge status={blocker.status} />
                        <span className="text-xs uppercase tracking-wide text-muted-foreground">{blocker.category}</span>
                      </div>
                      <p className="mt-2 text-sm">{blocker.message}</p>
                      <p className="mt-2 text-xs text-muted-foreground">Opened {formatTimestamp(blocker.created_at)}</p>
                    </div>
                  ))
                )}
              </div>
            </div>

            <div className="overflow-hidden rounded-2xl border bg-background">
              <div className="border-b px-5 py-3">
                <h2 className="text-sm font-semibold">Links</h2>
              </div>
              <div className="space-y-3 px-5 py-4">
                {workflow.links.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No linked artifacts recorded.</p>
                ) : (
                  workflow.links.map((link) => (
                    <div key={`${link.kind}:${link.ref ?? link.url ?? link.label}`} className="rounded-xl border bg-muted/20 p-4">
                      <div className="flex items-center justify-between gap-2">
                        {link.status ? <StatusBadge status={link.status} /> : <span className="text-xs uppercase tracking-wide text-muted-foreground">{link.kind.replace(/_/g, " ")}</span>}
                        {link.kind === "run" && link.ref ? (
                          <Link className="inline-flex items-center gap-1 text-sm text-primary hover:underline" href={`/${encodeURIComponent(tenantId)}/runs/${encodeURIComponent(link.ref)}`}>
                            Open
                            <ExternalLink className="h-3.5 w-3.5" />
                          </Link>
                        ) : link.url ? (
                          <Link className="inline-flex items-center gap-1 text-sm text-primary hover:underline" href={link.url}>
                            Open
                            <ExternalLink className="h-3.5 w-3.5" />
                          </Link>
                        ) : null}
                      </div>
                      <p className="mt-2 text-sm">{link.label}</p>
                      <p className="mt-2 text-xs text-muted-foreground">{link.ref ?? "No external reference recorded."}</p>
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        </>
      ) : (
        <div className="rounded-2xl border bg-background px-5 py-12 text-center text-sm text-muted-foreground">
          {loading ? "Loading execution…" : "Execution not found."}
        </div>
      )}
    </div>
  );
}
