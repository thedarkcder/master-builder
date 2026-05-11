"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft } from "lucide-react";

import { WorkflowFlowDiagram } from "@/components/workflow-flow-diagram";
import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatTimestamp } from "@/lib/datetime";
import { getWorkflowType, type WorkflowTypeDetailRecord } from "@/lib/api";
import { cn } from "@/lib/utils";

function impactLabel(completionRequired: boolean): string {
  return completionRequired ? "Must finish" : "Supporting";
}

function isSupportingOperation(operation: WorkflowTypeDetailRecord["operations"][number]): boolean {
  return operation.completion_required === false;
}

export default function TenantWorkflowTypeDetailPage() {
  const params = useParams<{ tenantId: string; workflowTypeKey: string }>();
  const { credentials, ready } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);
  const workflowTypeKey = decodeURIComponent(params.workflowTypeKey);

  const [workflowType, setWorkflowType] = useState<WorkflowTypeDetailRecord | null>(null);
  const [loading, setLoading] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [activeTab, setActiveTab] = useState<"settings" | "operations-flow" | "recent-executions">("settings");

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

  const flowNodes = useMemo(
    () =>
      (workflowType?.operations ?? []).map((operation) => ({
        key: operation.operation_type,
        operationType: operation.operation_type,
        label: operation.label,
        detail: null,
        impact: isSupportingOperation(operation) ? "Supporting" : impactLabel(operation.completion_required),
        completionRequired: !isSupportingOperation(operation),
        after: operation.after,
        supports: operation.supports,
      })),
    [workflowType?.operations],
  );

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <Button variant="ghost" size="sm" asChild className="h-8">
          <Link href={`/${encodeURIComponent(tenantId)}/workflows`}>
            <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />
            Back to workflows
          </Link>
        </Button>
      </div>

      {statusLine ? <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p> : null}

      {workflowType ? (
        <>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Workflow</p>
              <p className="mt-2 text-sm font-semibold">{workflowType.label}</p>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Engine</p>
              <div className="mt-2">
                <StatusBadge status={workflowType.orchestration_backend} />
              </div>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Steps</p>
              <p className="mt-2 text-sm font-semibold">{workflowType.operations.length}</p>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Executions</p>
              <p className="mt-2 text-sm font-semibold">{workflowType.execution_count}</p>
              <p className="mt-1 text-xs text-muted-foreground">
                {workflowType.latest_execution_at ? formatTimestamp(workflowType.latest_execution_at) : "No executions yet"}
              </p>
            </div>
          </div>

          <div className="space-y-4">
            <div className="overflow-x-auto border-b">
              <nav className="-mb-px flex min-w-max gap-0" aria-label="Workflow detail tabs">
                {[
                  { key: "settings", label: "Settings" },
                  { key: "operations-flow", label: "Operations flow" },
                  { key: "recent-executions", label: "Recent executions" },
                ].map((tab) => {
                  const selected = activeTab === tab.key;
                  return (
                    <button
                      key={tab.key}
                      type="button"
                      onClick={() => setActiveTab(tab.key as "settings" | "operations-flow" | "recent-executions")}
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

            {activeTab === "settings" ? (
              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="grid gap-4 px-5 py-4 text-sm md:grid-cols-2 xl:grid-cols-4">
                  <div>
                    <p className="font-medium">Definition source</p>
                    <p className="text-muted-foreground">Code decorators</p>
                  </div>
                  <div>
                    <p className="font-medium">Engine</p>
                    <p className="text-muted-foreground">{workflowType.orchestration_backend}</p>
                  </div>
                  <div>
                    <p className="font-medium">Manual retry</p>
                    <p className="text-muted-foreground">{workflowType.retry_policy.manual_retry_enabled ? "Enabled" : "Disabled"}</p>
                  </div>
                  <div>
                    <p className="font-medium">Retry policy</p>
                    <p className="text-muted-foreground">
                      {workflowType.retry_policy.max_attempts} attempts, {workflowType.retry_policy.initial_interval_seconds}s start, {workflowType.retry_policy.max_interval_seconds}s max
                    </p>
                  </div>
                </div>
              </div>
            ) : activeTab === "operations-flow" ? (
              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="px-5 py-4">
                  <WorkflowFlowDiagram nodes={flowNodes} emptyLabel="No steps defined." orientation="vertical" />
                </div>
              </div>
            ) : (
              <div className="overflow-hidden rounded-2xl border bg-background">
                {workflowType.recent_executions.length === 0 ? (
                  <div className="px-5 py-10 text-sm text-muted-foreground">No executions recorded.</div>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Execution</TableHead>
                        <TableHead>Status</TableHead>
                        <TableHead>Created</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {workflowType.recent_executions.map((execution) => (
                        <TableRow key={execution.execution_id}>
                          <TableCell className="font-medium">
                            <Link
                              className="text-primary hover:underline"
                              href={`/${encodeURIComponent(tenantId)}/executions/${encodeURIComponent(execution.execution_id)}`}
                            >
                              {execution.display_name?.trim() || execution.source_ref}
                            </Link>
                          </TableCell>
                          <TableCell>
                            <StatusBadge status={execution.status} />
                          </TableCell>
                          <TableCell className="whitespace-nowrap text-sm text-muted-foreground">{formatTimestamp(execution.created_at)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </div>
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
