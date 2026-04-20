"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft, RefreshCw } from "lucide-react";

import { WorkflowFlowDiagram } from "@/components/workflow-flow-diagram";
import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatTimestamp } from "@/lib/datetime";
import { getWorkflowType, updateWorkflowType, type WorkflowRetryPolicyRecord, type WorkflowTypeDetailRecord } from "@/lib/api";
import { cn } from "@/lib/utils";

const ENGINE_OPTIONS = [
  { value: "legacy", label: "Legacy" },
  { value: "temporal", label: "Temporal" },
  { value: "database", label: "Database" },
] as const;

const KNOWN_ERROR_CATEGORIES = [
  "authorization_failed",
  "content_limit",
  "contract_invalid",
  "missing_input",
  "transient_external_failure",
] as const;

function normalizeRetryPolicy(config: WorkflowRetryPolicyRecord): WorkflowRetryPolicyRecord {
  return {
    manual_retry_enabled: Boolean(config.manual_retry_enabled),
    max_attempts: Math.max(1, Number(config.max_attempts || 1)),
    initial_interval_seconds: Math.max(0, Number(config.initial_interval_seconds || 0)),
    max_interval_seconds: Math.max(0, Number(config.max_interval_seconds || 0)),
    backoff_coefficient: Math.max(1, Number(config.backoff_coefficient || 1)),
    non_retryable_error_categories: [...config.non_retryable_error_categories].sort(),
  };
}

function impactLabel(completionRequired: boolean): string {
  return completionRequired ? "Must finish" : "Supporting";
}

export default function TenantWorkflowTypeDetailPage() {
  const params = useParams<{ tenantId: string; workflowTypeKey: string }>();
  const { credentials, ready } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);
  const workflowTypeKey = decodeURIComponent(params.workflowTypeKey);

  const [workflowType, setWorkflowType] = useState<WorkflowTypeDetailRecord | null>(null);
  const [draft, setDraft] = useState<WorkflowTypeDetailRecord | null>(null);
  const [retryPolicy, setRetryPolicy] = useState<WorkflowRetryPolicyRecord | null>(null);
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
      setRetryPolicy(normalizeRetryPolicy(payload.retry_policy));
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to load workflow: ${(error as Error).message}`);
      setWorkflowType(null);
      setDraft(null);
      setRetryPolicy(null);
    } finally {
      setLoading(false);
    }
  }, [credentials, tenantId, workflowTypeKey]);

  useEffect(() => {
    if (ready && credentials) void loadWorkflowType();
  }, [ready, credentials, loadWorkflowType]);

  const updateRetryField = useCallback(
    (
      field:
        | "manual_retry_enabled"
        | "max_attempts"
        | "initial_interval_seconds"
        | "max_interval_seconds"
        | "backoff_coefficient",
      value: boolean | string,
    ) => {
      setRetryPolicy((current) => {
        if (!current) return current;
        if (field === "manual_retry_enabled" && typeof value === "boolean") {
          return { ...current, manual_retry_enabled: value };
        }
        if (field === "backoff_coefficient" && typeof value === "string") {
          return { ...current, backoff_coefficient: Number.parseFloat(value || "1") || 1 };
        }
        if (typeof value === "string") {
          return { ...current, [field]: Number.parseInt(value || "0", 10) || 0 };
        }
        return current;
      });
    },
    [],
  );

  const toggleNonRetryableCategory = useCallback((value: string) => {
    setRetryPolicy((current) => {
      if (!current) return current;
      const selected = current.non_retryable_error_categories.includes(value);
      return {
        ...current,
        non_retryable_error_categories: selected
          ? current.non_retryable_error_categories.filter((item) => item !== value)
          : [...current.non_retryable_error_categories, value].sort(),
      };
    });
  }, []);

  const saveWorkflowType = useCallback(async () => {
    if (!credentials || !draft || !retryPolicy) return;
    setSaving(true);
    try {
      const payload = await updateWorkflowType(credentials, workflowTypeKey, {
        orchestration_backend: draft.orchestration_backend,
        engine_config: draft.engine_config,
        retry_policy: normalizeRetryPolicy(retryPolicy),
      });
      setWorkflowType(payload);
      setDraft(payload);
      setRetryPolicy(normalizeRetryPolicy(payload.retry_policy));
      setStatusLine("Workflow saved.");
    } catch (error) {
      setStatusLine(`Failed to save workflow: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }, [credentials, draft, retryPolicy, workflowTypeKey]);

  const flowNodes = useMemo(
    () =>
      (workflowType?.operations ?? []).map((operation) => ({
        key: operation.operation_type,
        label: operation.label,
        detail: null,
        impact: impactLabel(operation.completion_required),
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
        <Button variant="outline" size="sm" className="h-8" onClick={() => void loadWorkflowType()} disabled={loading}>
          <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", loading && "animate-spin")} />
          Refresh
        </Button>
      </div>

      {statusLine ? <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p> : null}

      {workflowType && draft && retryPolicy ? (
        <>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Workflow</p>
              <p className="mt-2 text-sm font-semibold">{workflowType.label}</p>
              <p className="mt-1 text-xs text-muted-foreground">{workflowType.key}</p>
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

          <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <div className="space-y-4">
              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="border-b px-5 py-3">
                  <h2 className="text-sm font-semibold">Workflow settings</h2>
                </div>
                <div className="space-y-4 px-5 py-4">
                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">Engine</span>
                    <select
                      className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                      value={draft.orchestration_backend}
                      onChange={(event) => setDraft((current) => (current ? { ...current, orchestration_backend: event.target.value } : current))}
                    >
                      {ENGINE_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>

                  <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
                    <label className="space-y-1 text-sm">
                      <span className="text-muted-foreground">Manual retry</span>
                      <select
                        className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                        value={retryPolicy.manual_retry_enabled ? "enabled" : "disabled"}
                        onChange={(event) => updateRetryField("manual_retry_enabled", event.target.value === "enabled")}
                      >
                        <option value="enabled">Enabled</option>
                        <option value="disabled">Disabled</option>
                      </select>
                    </label>
                    <label className="space-y-1 text-sm">
                      <span className="text-muted-foreground">Max attempts</span>
                      <Input type="number" min={1} value={String(retryPolicy.max_attempts)} onChange={(event) => updateRetryField("max_attempts", event.target.value)} />
                    </label>
                    <label className="space-y-1 text-sm">
                      <span className="text-muted-foreground">Start delay (s)</span>
                      <Input type="number" min={0} value={String(retryPolicy.initial_interval_seconds)} onChange={(event) => updateRetryField("initial_interval_seconds", event.target.value)} />
                    </label>
                    <label className="space-y-1 text-sm">
                      <span className="text-muted-foreground">Max delay (s)</span>
                      <Input type="number" min={0} value={String(retryPolicy.max_interval_seconds)} onChange={(event) => updateRetryField("max_interval_seconds", event.target.value)} />
                    </label>
                    <label className="space-y-1 text-sm">
                      <span className="text-muted-foreground">Backoff</span>
                      <Input type="number" min={1} step="0.1" value={String(retryPolicy.backoff_coefficient)} onChange={(event) => updateRetryField("backoff_coefficient", event.target.value)} />
                    </label>
                  </div>

                  <div className="space-y-2">
                    <p className="text-sm font-medium">Do not retry</p>
                    <div className="grid gap-2 md:grid-cols-2">
                      {KNOWN_ERROR_CATEGORIES.map((category) => (
                        <label key={category} className="flex items-center gap-2 rounded-xl border px-3 py-2 text-sm">
                          <input
                            type="checkbox"
                            checked={retryPolicy.non_retryable_error_categories.includes(category)}
                            onChange={() => toggleNonRetryableCategory(category)}
                          />
                          <span>{category}</span>
                        </label>
                      ))}
                    </div>
                  </div>

                  <div className="flex justify-end">
                    <Button onClick={() => void saveWorkflowType()} disabled={saving} size="sm">
                      {saving ? "Saving…" : "Save"}
                    </Button>
                  </div>
                </div>
              </div>

              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="border-b px-5 py-3">
                  <h2 className="text-sm font-semibold">Definition flow</h2>
                </div>
                <div className="px-5 py-4">
                  <WorkflowFlowDiagram nodes={flowNodes} emptyLabel="No steps defined." />
                </div>
              </div>
            </div>

            <div className="space-y-4">
              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="border-b px-5 py-3">
                  <h2 className="text-sm font-semibold">Recent executions</h2>
                </div>
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
                            <StatusBadge status={execution.status} />
                          </TableCell>
                          <TableCell className="whitespace-nowrap text-sm text-muted-foreground">{formatTimestamp(execution.created_at)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </div>

            </div>
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
