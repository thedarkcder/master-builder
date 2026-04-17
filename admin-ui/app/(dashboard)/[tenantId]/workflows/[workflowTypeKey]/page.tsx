"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft, RefreshCw } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatTimestamp } from "@/lib/datetime";
import { getWorkflowType, type WorkflowTypeDetailRecord } from "@/lib/api";
import { cn } from "@/lib/utils";

export default function TenantWorkflowTypeDetailPage() {
  const params = useParams<{ tenantId: string; workflowTypeKey: string }>();
  const { credentials, ready } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);
  const workflowTypeKey = decodeURIComponent(params.workflowTypeKey);

  const [workflowType, setWorkflowType] = useState<WorkflowTypeDetailRecord | null>(null);
  const [loading, setLoading] = useState(false);
  const [statusLine, setStatusLine] = useState("");

  const loadWorkflowType = useCallback(async () => {
    if (!credentials) return;
    setLoading(true);
    try {
      const payload = await getWorkflowType(credentials, workflowTypeKey, { tenantId });
      setWorkflowType(payload);
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to load workflow: ${(error as Error).message}`);
      setWorkflowType(null);
    } finally {
      setLoading(false);
    }
  }, [credentials, tenantId, workflowTypeKey]);

  useEffect(() => {
    if (ready && credentials) void loadWorkflowType();
  }, [ready, credentials, loadWorkflowType]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <Button variant="ghost" size="sm" asChild className="h-8">
          <Link href={`/${encodeURIComponent(tenantId)}/workflows`}>
            <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />
            Back to workflows
          </Link>
        </Button>
        <Button variant="outline" size="sm" className="h-8" onClick={() => void loadWorkflowType()} disabled={loading}>
          <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", loading && "animate-spin")} />
          Refresh
        </Button>
      </div>

      {statusLine ? (
        <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p>
      ) : null}

      {workflowType ? (
        <>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Workflow</p>
              <p className="mt-2 text-sm font-semibold">{workflowType.label}</p>
              <p className="mt-1 text-xs text-muted-foreground">{workflowType.key}</p>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Operations</p>
              <p className="mt-2 text-sm font-semibold">{workflowType.operations.length}</p>
              <p className="mt-1 text-xs text-muted-foreground">Defined engine operations</p>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Executions</p>
              <p className="mt-2 text-sm font-semibold">{workflowType.execution_count}</p>
              <p className="mt-1 text-xs text-muted-foreground">
                {workflowType.latest_execution_at ? `Latest ${formatTimestamp(workflowType.latest_execution_at)}` : "No executions yet"}
              </p>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Execution modes</p>
              <p className="mt-2 text-sm font-semibold">{workflowType.execution_modes.join(", ") || "—"}</p>
              <p className="mt-1 text-xs text-muted-foreground">Available execution entry paths</p>
            </div>
          </div>

          {workflowType.description ? (
            <div className="rounded-2xl border bg-background px-5 py-4 text-sm text-muted-foreground">
              {workflowType.description}
            </div>
          ) : null}

          <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <div className="overflow-hidden rounded-2xl border bg-background">
              <div className="border-b px-5 py-3">
                <h2 className="text-sm font-semibold">Operations</h2>
              </div>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Operation</TableHead>
                    <TableHead>Required</TableHead>
                    <TableHead>Retry policy</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {workflowType.operations.map((operation) => (
                    <TableRow key={operation.operation_type}>
                      <TableCell className="font-medium">
                        <p>{operation.label}</p>
                        <p className="text-xs text-muted-foreground">{operation.operation_type}</p>
                        {operation.description ? (
                          <p className="mt-1 text-sm text-muted-foreground">{operation.description}</p>
                        ) : null}
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        {operation.required ? "Required" : "Optional"}
                      </TableCell>
                      <TableCell className="max-w-[420px] text-sm text-muted-foreground">
                        {operation.retry_policy}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>

            <div className="space-y-4">
              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="border-b px-5 py-3">
                  <h2 className="text-sm font-semibold">Conditional paths</h2>
                </div>
                <div className="space-y-2 px-5 py-4">
                  {workflowType.conditional_paths.map((path) => (
                    <div key={path} className="rounded-xl border bg-muted/20 px-3 py-2 text-sm text-muted-foreground">
                      {path}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>

          <div className="overflow-hidden rounded-2xl border bg-background">
            <div className="border-b px-5 py-3">
              <h2 className="text-sm font-semibold">Recent executions</h2>
            </div>
            {workflowType.recent_executions.length === 0 ? (
              <div className="px-5 py-10 text-sm text-muted-foreground">No executions recorded for this workflow yet.</div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Execution</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Failure</TableHead>
                    <TableHead>Next</TableHead>
                    <TableHead>Created</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {workflowType.recent_executions.map((execution) => (
                    <TableRow key={execution.workflow_id}>
                      <TableCell className="font-medium">
                        <Link
                          className="text-primary hover:underline"
                          href={`/${encodeURIComponent(tenantId)}/executions/${encodeURIComponent(execution.workflow_id)}`}
                        >
                          {execution.issue_summary?.trim() || execution.issue_key}
                        </Link>
                        <p className="text-xs text-muted-foreground">{execution.workflow_id}</p>
                      </TableCell>
                      <TableCell>
                        <div className="space-y-1">
                          <StatusBadge status={execution.status} />
                          {execution.waiting_on ? (
                            <p className="text-xs text-muted-foreground">{execution.waiting_on.replace(/_/g, " ")}</p>
                          ) : null}
                        </div>
                      </TableCell>
                      <TableCell className="max-w-[420px] text-sm text-muted-foreground">
                        {execution.failure_reason?.trim() || "—"}
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">{execution.next_step?.trim() || "—"}</TableCell>
                      <TableCell className="whitespace-nowrap text-sm text-muted-foreground">
                        {formatTimestamp(execution.created_at)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </div>
        </>
      ) : (
        <div className="rounded-2xl border bg-background px-5 py-12 text-center text-sm text-muted-foreground">
          {loading ? "Loading workflow…" : "Workflow not found."}
        </div>
      )}
    </div>
  );
}
