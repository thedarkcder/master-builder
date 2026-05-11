"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { SlidersHorizontal, X } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatTimestamp } from "@/lib/datetime";
import { listWorkflows, type WorkflowRecord } from "@/lib/api";
import { cn } from "@/lib/utils";

const WORKFLOW_STATUS_OPTIONS = ["queued", "running", "waiting_for_input", "completed", "failed", "cancelled"] as const;

function statusBorderClass(status: string): string {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "completed") return "border-l-success";
  if (normalized === "failed" || normalized === "blocked") return "border-l-destructive";
  if (normalized === "running" || normalized === "waiting_for_input") return "border-l-warning";
  if (normalized === "queued") return "border-l-info";
  return "border-l-border";
}

function summarizeFailure(workflow: WorkflowRecord): string {
  if (workflow.failure_reason?.trim()) {
    return workflow.failure_reason.trim();
  }
  const latestAttempt = workflow.operations.flatMap((operation) => operation.attempts).sort((left, right) => {
    const leftTime = new Date(left.finished_at ?? left.started_at ?? 0).getTime();
    const rightTime = new Date(right.finished_at ?? right.started_at ?? 0).getTime();
    return rightTime - leftTime;
  })[0];
  return latestAttempt?.error_message?.trim() || "—";
}

function retryableState(workflow: WorkflowRecord): string {
  const retryableOperations = workflow.operations.filter((operation) => operation.can_retry);
  if (retryableOperations.length === 0) {
    return "No";
  }
  return retryableOperations.some((operation) => operation.status === "retrying") ? "Retrying" : "Yes";
}

function nextStateLabel(workflow: WorkflowRecord): string {
  if (workflow.waiting_on === "human_input") {
    return "Waiting on human input";
  }
  return workflow.next_step?.trim() || "—";
}

export default function TenantExecutionsPage() {
  const params = useParams<{ tenantId: string }>();
  const { credentials, ready } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);

  const [workflows, setWorkflows] = useState<WorkflowRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [issueFilter, setIssueFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<25 | 50 | 100>(25);

  const loadWorkflows = useCallback(async () => {
    if (!credentials) return;
    setLoading(true);
    try {
      const payload = await listWorkflows(credentials, {
        tenantId,
        issue: issueFilter || undefined,
        status: statusFilter === "all" ? undefined : statusFilter,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      });
      setWorkflows(payload);
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to load executions: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [credentials, issueFilter, page, pageSize, statusFilter, tenantId]);

  useEffect(() => {
    if (ready && credentials) void loadWorkflows();
  }, [ready, credentials, loadWorkflows]);

  const hasFilters = useMemo(() => issueFilter.trim().length > 0 || statusFilter !== "all", [issueFilter, statusFilter]);

  function clearFilters() {
    setIssueFilter("");
    setStatusFilter("all");
    setPage(1);
  }

  return (
    <div className="space-y-4">
      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="p-5">
          <div className="overflow-x-auto -mx-1 px-1">
            <div className="flex min-w-max flex-nowrap items-end gap-2 md:min-w-0 md:flex-wrap">
              <div className="flex shrink-0 items-center gap-1.5 text-muted-foreground">
                <SlidersHorizontal className="h-4 w-4" />
                <span className="text-[11px] font-medium uppercase tracking-[0.18em]">Filters</span>
              </div>

              <Input
                className="h-8 w-40 text-sm"
                value={issueFilter}
                onChange={(event) => {
                  setIssueFilter(event.target.value);
                  setPage(1);
                }}
                placeholder="Issue / summary"
              />
              <select
                className="h-8 w-36 rounded-md border border-input bg-background px-2 text-sm"
                value={statusFilter}
                onChange={(event) => {
                  setStatusFilter(event.target.value);
                  setPage(1);
                }}
              >
                <option value="all">Status: Any</option>
                {WORKFLOW_STATUS_OPTIONS.map((status) => (
                  <option key={status} value={status}>
                    {status}
                  </option>
                ))}
              </select>

              <div className="ml-auto flex shrink-0 items-center gap-1.5">
                {hasFilters ? (
                  <Button variant="ghost" size="sm" className="h-8 text-xs text-muted-foreground" onClick={clearFilters}>
                    <X className="mr-1 h-3 w-3" />
                    Clear
                  </Button>
                ) : null}
                <select
                  className="h-8 rounded-md border border-input bg-background px-2 text-xs"
                  value={String(pageSize)}
                  onChange={(event) => {
                    setPageSize(Number(event.target.value) as 25 | 50 | 100);
                    setPage(1);
                  }}
                >
                  <option value="25">25 / page</option>
                  <option value="50">50 / page</option>
                  <option value="100">100 / page</option>
                </select>
              </div>
            </div>
          </div>
        </div>

        {statusLine ? (
          <div className="border-t px-5 py-3 text-sm text-muted-foreground">{statusLine}</div>
        ) : null}

        {workflows.length === 0 && !loading ? (
          <div className="border-t px-5 py-12 text-center text-sm text-muted-foreground">
            No executions found for the current filters.
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Execution</TableHead>
                <TableHead>Workflow type</TableHead>
                <TableHead>Issue</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Failure</TableHead>
                <TableHead>Retryable</TableHead>
                <TableHead>Created</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody className={cn(loading && "pointer-events-none opacity-50 transition-opacity")}>
              {workflows.map((workflow) => (
                <TableRow key={workflow.execution_id} className={cn("border-l-2", statusBorderClass(workflow.status))}>
                  <TableCell className="font-medium">
                    <Link
                      className="text-primary hover:underline"
                      href={`/${encodeURIComponent(tenantId)}/executions/${encodeURIComponent(workflow.execution_id)}`}
                    >
                      {workflow.display_name?.trim() || workflow.source_ref}
                    </Link>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    <p>{workflow.workflow_type.label}</p>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">{workflow.source_ref}</TableCell>
                  <TableCell>
                    <div className="space-y-1">
                      <StatusBadge status={workflow.current_state} />
                      {workflow.waiting_on ? (
                        <p className="text-xs text-muted-foreground">{workflow.waiting_on.replace(/_/g, " ")}</p>
                      ) : null}
                    </div>
                  </TableCell>
                  <TableCell className="max-w-[420px] text-sm text-muted-foreground">
                    <div className="space-y-1">
                      <p className="line-clamp-3">{summarizeFailure(workflow)}</p>
                      <p className="text-xs text-muted-foreground">Next: {nextStateLabel(workflow)}</p>
                    </div>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">{retryableState(workflow)}</TableCell>
                  <TableCell className="whitespace-nowrap text-sm text-muted-foreground">
                    {formatTimestamp(workflow.created_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}

        <div className="flex items-center justify-between gap-2 border-t px-5 py-3 text-sm text-muted-foreground">
          <span>
            {workflows.length} execution{workflows.length === 1 ? "" : "s"}
          </span>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" className="h-8" disabled={loading || page <= 1} onClick={() => setPage((value) => value - 1)}>
              Previous
            </Button>
            <span>Page {page}</span>
            <Button
              variant="outline"
              size="sm"
              className="h-8"
              disabled={loading || workflows.length < pageSize}
              onClick={() => setPage((value) => value + 1)}
            >
              Next
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
