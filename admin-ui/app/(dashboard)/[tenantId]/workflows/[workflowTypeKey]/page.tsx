"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft, RefreshCw } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { formatTimestamp } from "@/lib/datetime";
import { getWorkflowType, updateWorkflowType, type WorkflowTypeDetailRecord } from "@/lib/api";
import { cn } from "@/lib/utils";

export default function TenantWorkflowTypeDetailPage() {
  const params = useParams<{ tenantId: string; workflowTypeKey: string }>();
  const { credentials, ready } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);
  const workflowTypeKey = decodeURIComponent(params.workflowTypeKey);

  const [workflowType, setWorkflowType] = useState<WorkflowTypeDetailRecord | null>(null);
  const [draft, setDraft] = useState<WorkflowTypeDetailRecord | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [statusLine, setStatusLine] = useState("");

  const loadWorkflowType = useCallback(async () => {
    if (!credentials) return;
    setLoading(true);
    try {
      const payload = await getWorkflowType(credentials, workflowTypeKey, { tenantId });
      setWorkflowType(payload);
      setDraft(payload);
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to load workflow: ${(error as Error).message}`);
      setWorkflowType(null);
      setDraft(null);
    } finally {
      setLoading(false);
    }
  }, [credentials, tenantId, workflowTypeKey]);

  useEffect(() => {
    if (ready && credentials) void loadWorkflowType();
  }, [ready, credentials, loadWorkflowType]);

  const updateDraftBackend = useCallback((value: string) => {
    setDraft((current) => (current ? { ...current, orchestration_backend: value } : current));
  }, []);

  const updateDraftTemporalField = useCallback(
    (
      field:
        | "workflow_name"
        | "task_queue"
        | "activity_start_to_close_timeout_seconds"
        | "human_input_resume_timeout_seconds",
      value: string,
    ) => {
      setDraft((current) => {
        if (!current) return current;
        const temporal = current.engine_config.temporal ?? {
          workflow_name: "",
          task_queue: "",
          activity_start_to_close_timeout_seconds: 7200,
          human_input_resume_timeout_seconds: 7200,
        };
        return {
          ...current,
          engine_config: {
            ...current.engine_config,
            temporal: {
              ...temporal,
              [field]:
                field.endsWith("_seconds")
                  ? Number.parseInt(value || "0", 10) || 0
                  : value,
            },
          },
        };
      });
    },
    [],
  );

  const updateOperationField = useCallback(
    (operationType: string, field: "retry_policy", value: string) => {
      setDraft((current) => {
        if (!current) return current;
        return {
          ...current,
          operations: current.operations.map((operation) =>
            operation.operation_type === operationType ? { ...operation, [field]: value } : operation,
          ),
        };
      });
    },
    [],
  );

  const updateOperationRetryConfig = useCallback(
    (
      operationType: string,
      field:
        | "manual_retry_enabled"
        | "max_attempts"
        | "initial_interval_seconds"
        | "max_interval_seconds"
        | "backoff_coefficient"
        | "non_retryable_error_categories",
      value: string | boolean,
    ) => {
      setDraft((current) => {
        if (!current) return current;
        return {
          ...current,
          operations: current.operations.map((operation) => {
            if (operation.operation_type !== operationType) return operation;
            const nextConfig = { ...operation.retry_policy_config };
            if (field === "manual_retry_enabled" && typeof value === "boolean") {
              nextConfig.manual_retry_enabled = value;
            } else if (field === "non_retryable_error_categories" && typeof value === "string") {
              nextConfig.non_retryable_error_categories = value
                .split(",")
                .map((item) => item.trim())
                .filter(Boolean);
            } else if (field === "backoff_coefficient" && typeof value === "string") {
              nextConfig.backoff_coefficient = Number.parseFloat(value || "1") || 1;
            } else if (field === "max_attempts" && typeof value === "string") {
              nextConfig.max_attempts = Number.parseInt(value || "0", 10) || 0;
            } else if (field === "initial_interval_seconds" && typeof value === "string") {
              nextConfig.initial_interval_seconds = Number.parseInt(value || "0", 10) || 0;
            } else if (field === "max_interval_seconds" && typeof value === "string") {
              nextConfig.max_interval_seconds = Number.parseInt(value || "0", 10) || 0;
            }
            return { ...operation, retry_policy_config: nextConfig };
          }),
        };
      });
    },
    [],
  );

  const saveWorkflowType = useCallback(async () => {
    if (!credentials || !draft) return;
    setSaving(true);
    try {
      const payload = await updateWorkflowType(credentials, workflowTypeKey, {
        orchestration_backend: draft.orchestration_backend,
        engine_config: draft.engine_config,
        operations: draft.operations.map((operation) => ({
          operation_type: operation.operation_type,
          retry_policy: operation.retry_policy,
          retry_policy_config: operation.retry_policy_config,
        })),
      });
      setWorkflowType(payload);
      setDraft(payload);
      setStatusLine("Workflow configuration saved.");
    } catch (error) {
      setStatusLine(`Failed to save workflow: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }, [credentials, draft, workflowTypeKey]);

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

      {workflowType && draft ? (
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
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Backend</p>
              <p className="mt-2 text-sm font-semibold">{workflowType.orchestration_backend}</p>
              <p className="mt-1 text-xs text-muted-foreground">Workflow engine implementation</p>
            </div>
          </div>

          {workflowType.description ? (
            <div className="rounded-2xl border bg-background px-5 py-4 text-sm text-muted-foreground">
              {workflowType.description}
            </div>
          ) : null}

          <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <div className="space-y-4">
              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="border-b px-5 py-3">
                  <h2 className="text-sm font-semibold">Workflow config</h2>
                </div>
                <div className="grid gap-4 px-5 py-4 md:grid-cols-2">
                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">Backend</span>
                    <Input value={draft.orchestration_backend} onChange={(event) => updateDraftBackend(event.target.value)} />
                  </label>
                  {draft.orchestration_backend === "temporal" ? (
                    <>
                      <label className="space-y-1 text-sm">
                        <span className="text-muted-foreground">Temporal workflow name</span>
                        <Input
                          value={draft.engine_config.temporal?.workflow_name ?? ""}
                          onChange={(event) => updateDraftTemporalField("workflow_name", event.target.value)}
                        />
                      </label>
                      <label className="space-y-1 text-sm">
                        <span className="text-muted-foreground">Task queue</span>
                        <Input
                          value={draft.engine_config.temporal?.task_queue ?? ""}
                          onChange={(event) => updateDraftTemporalField("task_queue", event.target.value)}
                        />
                      </label>
                      <label className="space-y-1 text-sm">
                        <span className="text-muted-foreground">Activity timeout (seconds)</span>
                        <Input
                          type="number"
                          min={1}
                          value={String(draft.engine_config.temporal?.activity_start_to_close_timeout_seconds ?? 0)}
                          onChange={(event) => updateDraftTemporalField("activity_start_to_close_timeout_seconds", event.target.value)}
                        />
                      </label>
                      <label className="space-y-1 text-sm">
                        <span className="text-muted-foreground">Human input resume timeout (seconds)</span>
                        <Input
                          type="number"
                          min={1}
                          value={String(draft.engine_config.temporal?.human_input_resume_timeout_seconds ?? 0)}
                          onChange={(event) => updateDraftTemporalField("human_input_resume_timeout_seconds", event.target.value)}
                        />
                      </label>
                    </>
                  ) : (
                    <div className="rounded-xl border bg-muted/20 px-3 py-2 text-sm text-muted-foreground md:col-span-2">
                      This workflow type does not currently use Temporal-specific engine settings.
                    </div>
                  )}
                </div>
              </div>

              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="border-b px-5 py-3">
                  <h2 className="text-sm font-semibold">Operations</h2>
                </div>
                <div className="space-y-4 px-5 py-4">
                  {draft.operations.map((operation) => (
                    <div key={operation.operation_type} className="rounded-2xl border p-4">
                      <div className="mb-3">
                        <p className="text-sm font-semibold">{operation.label}</p>
                        <p className="text-xs text-muted-foreground">{operation.operation_type}</p>
                      </div>
                      <div className="grid gap-4">
                        <label className="space-y-1 text-sm">
                          <span className="text-muted-foreground">Retry policy summary</span>
                          <Textarea
                            value={operation.retry_policy}
                            onChange={(event) => updateOperationField(operation.operation_type, "retry_policy", event.target.value)}
                            className="min-h-[90px]"
                          />
                        </label>
                        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
                          <label className="space-y-1 text-sm">
                            <span className="text-muted-foreground">Max attempts</span>
                            <Input
                              type="number"
                              min={1}
                              value={String(operation.retry_policy_config.max_attempts)}
                              onChange={(event) => updateOperationRetryConfig(operation.operation_type, "max_attempts", event.target.value)}
                            />
                          </label>
                          <label className="space-y-1 text-sm">
                            <span className="text-muted-foreground">Initial interval (seconds)</span>
                            <Input
                              type="number"
                              min={0}
                              value={String(operation.retry_policy_config.initial_interval_seconds)}
                              onChange={(event) => updateOperationRetryConfig(operation.operation_type, "initial_interval_seconds", event.target.value)}
                            />
                          </label>
                          <label className="space-y-1 text-sm">
                            <span className="text-muted-foreground">Max interval (seconds)</span>
                            <Input
                              type="number"
                              min={0}
                              value={String(operation.retry_policy_config.max_interval_seconds)}
                              onChange={(event) => updateOperationRetryConfig(operation.operation_type, "max_interval_seconds", event.target.value)}
                            />
                          </label>
                          <label className="space-y-1 text-sm">
                            <span className="text-muted-foreground">Backoff coefficient</span>
                            <Input
                              type="number"
                              min={1}
                              step="0.1"
                              value={String(operation.retry_policy_config.backoff_coefficient)}
                              onChange={(event) => updateOperationRetryConfig(operation.operation_type, "backoff_coefficient", event.target.value)}
                            />
                          </label>
                          <label className="flex items-center gap-2 rounded-xl border px-3 py-2 text-sm">
                            <input
                              type="checkbox"
                              checked={operation.retry_policy_config.manual_retry_enabled}
                              onChange={(event) => updateOperationRetryConfig(operation.operation_type, "manual_retry_enabled", event.target.checked)}
                            />
                            Manual retry enabled
                          </label>
                        </div>
                        <label className="space-y-1 text-sm">
                          <span className="text-muted-foreground">Non-retryable failure categories</span>
                          <Input
                            value={operation.retry_policy_config.non_retryable_error_categories.join(", ")}
                            onChange={(event) =>
                              updateOperationRetryConfig(operation.operation_type, "non_retryable_error_categories", event.target.value)
                            }
                            placeholder="content_limit, contract_invalid"
                          />
                        </label>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>

            <div className="overflow-hidden rounded-2xl border bg-background">
              <div className="border-b px-5 py-3">
                <h2 className="text-sm font-semibold">Execution contract</h2>
              </div>
              <div className="space-y-4 px-5 py-4">
                <div>
                  <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Execution modes</p>
                  <p className="mt-2 text-sm text-muted-foreground">{workflowType.execution_modes.join(", ") || "—"}</p>
                </div>
                <div>
                  <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Conditional paths</p>
                  <div className="mt-2 space-y-2">
                    {workflowType.conditional_paths.map((path) => (
                      <div key={path} className="rounded-xl border bg-muted/20 px-3 py-2 text-sm text-muted-foreground">
                        {path}
                      </div>
                    ))}
                  </div>
                </div>
                <div className="pt-2">
                  <Button onClick={() => void saveWorkflowType()} disabled={saving} size="sm">
                    {saving ? "Saving…" : "Save workflow config"}
                  </Button>
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
