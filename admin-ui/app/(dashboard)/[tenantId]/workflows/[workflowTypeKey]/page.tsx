"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft, RefreshCw } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/ui/status-badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatTimestamp } from "@/lib/datetime";
import { getWorkflowType, updateWorkflowType, type WorkflowTypeDetailRecord } from "@/lib/api";
import { cn } from "@/lib/utils";

const ENGINE_OPTIONS = [
  { value: "legacy", label: "Legacy engine" },
  { value: "temporal", label: "Temporal" },
  { value: "database", label: "Database engine" },
] as const;

const KNOWN_ERROR_CATEGORIES = [
  "authorization_failed",
  "content_limit",
  "contract_invalid",
  "missing_input",
  "transient_external_failure",
] as const;

type RetryPolicyDraft = {
  manual_retry_enabled: boolean;
  max_attempts: number;
  initial_interval_seconds: number;
  max_interval_seconds: number;
  backoff_coefficient: number;
  non_retryable_error_categories: string[];
};

function normalizeRetryPolicy(config: RetryPolicyDraft): RetryPolicyDraft {
  return {
    manual_retry_enabled: Boolean(config.manual_retry_enabled),
    max_attempts: Math.max(1, Number(config.max_attempts || 1)),
    initial_interval_seconds: Math.max(0, Number(config.initial_interval_seconds || 0)),
    max_interval_seconds: Math.max(0, Number(config.max_interval_seconds || 0)),
    backoff_coefficient: Math.max(1, Number(config.backoff_coefficient || 1)),
    non_retryable_error_categories: [...config.non_retryable_error_categories].sort(),
  };
}

function sameRetryPolicy(left: RetryPolicyDraft, right: RetryPolicyDraft): boolean {
  const a = normalizeRetryPolicy(left);
  const b = normalizeRetryPolicy(right);
  return JSON.stringify(a) === JSON.stringify(b);
}

function inferWorkflowRetryPolicy(workflowType: WorkflowTypeDetailRecord): {
  draft: RetryPolicyDraft;
  mixed: boolean;
} {
  const [firstOperation] = workflowType.operations;
  const firstConfig = normalizeRetryPolicy(
    firstOperation?.retry_policy_config ?? {
      manual_retry_enabled: false,
      max_attempts: 1,
      initial_interval_seconds: 0,
      max_interval_seconds: 0,
      backoff_coefficient: 1,
      non_retryable_error_categories: [],
    },
  );
  const mixed = workflowType.operations.some(
    (operation) => !sameRetryPolicy(firstConfig, operation.retry_policy_config),
  );
  return { draft: firstConfig, mixed };
}

function buildRetryPolicySummary(config: RetryPolicyDraft): string {
  const normalized = normalizeRetryPolicy(config);
  const retryMode = normalized.manual_retry_enabled ? "Manual retry available" : "Manual retry disabled";
  const categories = normalized.non_retryable_error_categories.length
    ? `Non-retryable: ${normalized.non_retryable_error_categories.join(", ")}`
    : "No explicit non-retryable categories";
  return `${retryMode}. Up to ${normalized.max_attempts} attempts, start at ${normalized.initial_interval_seconds}s, cap at ${normalized.max_interval_seconds}s, backoff x${normalized.backoff_coefficient}. ${categories}.`;
}

function formatEngineLabel(value: string): string {
  return ENGINE_OPTIONS.find((option) => option.value === value)?.label ?? value;
}

export default function TenantWorkflowTypeDetailPage() {
  const params = useParams<{ tenantId: string; workflowTypeKey: string }>();
  const { credentials, ready } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);
  const workflowTypeKey = decodeURIComponent(params.workflowTypeKey);

  const [workflowType, setWorkflowType] = useState<WorkflowTypeDetailRecord | null>(null);
  const [draft, setDraft] = useState<WorkflowTypeDetailRecord | null>(null);
  const [sharedRetryPolicy, setSharedRetryPolicy] = useState<RetryPolicyDraft | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [hasMixedRetryPolicies, setHasMixedRetryPolicies] = useState(false);

  const loadWorkflowType = useCallback(async () => {
    if (!credentials) return;
    setLoading(true);
    try {
      const payload = await getWorkflowType(credentials, workflowTypeKey, { tenantId });
      const retryPolicy = inferWorkflowRetryPolicy(payload);
      setWorkflowType(payload);
      setDraft(payload);
      setSharedRetryPolicy(retryPolicy.draft);
      setHasMixedRetryPolicies(retryPolicy.mixed);
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to load workflow: ${(error as Error).message}`);
      setWorkflowType(null);
      setDraft(null);
      setSharedRetryPolicy(null);
      setHasMixedRetryPolicies(false);
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
        | "workflow_execution_timeout_seconds"
        | "workflow_run_timeout_seconds"
        | "activity_start_to_close_timeout_seconds"
        | "human_input_resume_timeout_seconds",
      value: string,
    ) => {
      setDraft((current) => {
        if (!current) return current;
        const temporal = current.engine_config.temporal ?? {
          workflow_name: "",
          task_queue: "",
          workflow_execution_timeout_seconds: 86400,
          workflow_run_timeout_seconds: 86400,
          activity_start_to_close_timeout_seconds: 7200,
          human_input_resume_timeout_seconds: 7200,
        };
        return {
          ...current,
          engine_config: {
            ...current.engine_config,
            temporal: {
              ...temporal,
              [field]: field.endsWith("_seconds") ? Number.parseInt(value || "0", 10) || 0 : value,
            },
          },
        };
      });
    },
    [],
  );

  const updateSharedRetryField = useCallback(
    (
      field:
        | "manual_retry_enabled"
        | "max_attempts"
        | "initial_interval_seconds"
        | "max_interval_seconds"
        | "backoff_coefficient",
      value: boolean | string,
    ) => {
      setSharedRetryPolicy((current) => {
        const base =
          current ?? {
            manual_retry_enabled: false,
            max_attempts: 1,
            initial_interval_seconds: 0,
            max_interval_seconds: 0,
            backoff_coefficient: 1,
            non_retryable_error_categories: [],
          };
        if (field === "manual_retry_enabled" && typeof value === "boolean") {
          return { ...base, manual_retry_enabled: value };
        }
        if (field === "backoff_coefficient" && typeof value === "string") {
          return { ...base, backoff_coefficient: Number.parseFloat(value || "1") || 1 };
        }
        if (typeof value === "string") {
          return { ...base, [field]: Number.parseInt(value || "0", 10) || 0 };
        }
        return base;
      });
    },
    [],
  );

  const toggleNonRetryableCategory = useCallback((value: string) => {
    setSharedRetryPolicy((current) => {
      if (!current) return current;
      const alreadySelected = current.non_retryable_error_categories.includes(value);
      const nextCategories = alreadySelected
        ? current.non_retryable_error_categories.filter((item) => item !== value)
        : [...current.non_retryable_error_categories, value];
      return { ...current, non_retryable_error_categories: nextCategories.sort() };
    });
  }, []);

  const saveWorkflowType = useCallback(async () => {
    if (!credentials || !draft || !sharedRetryPolicy) return;
    setSaving(true);
    try {
      const normalizedRetryPolicy = normalizeRetryPolicy(sharedRetryPolicy);
      const retryPolicySummary = buildRetryPolicySummary(normalizedRetryPolicy);
      const payload = await updateWorkflowType(credentials, workflowTypeKey, {
        orchestration_backend: draft.orchestration_backend,
        engine_config: draft.engine_config,
        operations: draft.operations.map((operation) => ({
          operation_type: operation.operation_type,
          retry_policy: retryPolicySummary,
          retry_policy_config: normalizedRetryPolicy,
        })),
      });
      const retryPolicy = inferWorkflowRetryPolicy(payload);
      setWorkflowType(payload);
      setDraft(payload);
      setSharedRetryPolicy(retryPolicy.draft);
      setHasMixedRetryPolicies(retryPolicy.mixed);
      setStatusLine("Workflow policy saved.");
    } catch (error) {
      setStatusLine(`Failed to save workflow: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }, [credentials, draft, sharedRetryPolicy, workflowTypeKey]);

  const retryPolicySummary = useMemo(
    () => (sharedRetryPolicy ? buildRetryPolicySummary(sharedRetryPolicy) : "—"),
    [sharedRetryPolicy],
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

      {statusLine ? (
        <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p>
      ) : null}

      {workflowType && draft && sharedRetryPolicy ? (
        <>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Workflow</p>
              <p className="mt-2 text-sm font-semibold">{workflowType.label}</p>
              <p className="mt-1 text-xs text-muted-foreground">{workflowType.key}</p>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Execution engine</p>
              <p className="mt-2 text-sm font-semibold">{formatEngineLabel(workflowType.orchestration_backend)}</p>
              <p className="mt-1 text-xs text-muted-foreground">
                {workflowType.orchestration_backend === "temporal" ? "Durable Temporal workflow" : "Configured in workflow catalog"}
              </p>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Retry policy</p>
              <p className="mt-2 text-sm font-semibold">
                {sharedRetryPolicy.max_attempts} attempts
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                {sharedRetryPolicy.manual_retry_enabled ? "Manual retry enabled" : "Manual retry disabled"}
              </p>
            </div>
            <div className="rounded-2xl border bg-background p-4">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Executions</p>
              <p className="mt-2 text-sm font-semibold">{workflowType.execution_count}</p>
              <p className="mt-1 text-xs text-muted-foreground">
                {workflowType.latest_execution_at ? `Latest ${formatTimestamp(workflowType.latest_execution_at)}` : "No executions yet"}
              </p>
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
                  <h2 className="text-sm font-semibold">Workflow settings</h2>
                </div>
                <div className="grid gap-4 px-5 py-4 md:grid-cols-2">
                  <label className="space-y-1 text-sm">
                    <span className="text-muted-foreground">Execution engine</span>
                    <select
                      className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                      value={draft.orchestration_backend}
                      onChange={(event) => updateDraftBackend(event.target.value)}
                    >
                      {ENGINE_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <div className="rounded-xl border bg-muted/20 px-3 py-2 text-sm text-muted-foreground">
                    <p className="font-medium text-foreground">Execution modes</p>
                    <p className="mt-1">{workflowType.lifecycle.execution_modes.join(", ") || "—"}</p>
                  </div>
                  {draft.orchestration_backend === "temporal" ? (
                    <>
                      <label className="space-y-1 text-sm">
                        <span className="text-muted-foreground">Temporal workflow</span>
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
                        <span className="text-muted-foreground">Execution timeout (seconds)</span>
                        <Input
                          type="number"
                          min={1}
                          value={String(draft.engine_config.temporal?.workflow_execution_timeout_seconds ?? 0)}
                          onChange={(event) => updateDraftTemporalField("workflow_execution_timeout_seconds", event.target.value)}
                        />
                      </label>
                      <label className="space-y-1 text-sm">
                        <span className="text-muted-foreground">Run timeout (seconds)</span>
                        <Input
                          type="number"
                          min={1}
                          value={String(draft.engine_config.temporal?.workflow_run_timeout_seconds ?? 0)}
                          onChange={(event) => updateDraftTemporalField("workflow_run_timeout_seconds", event.target.value)}
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
                        <span className="text-muted-foreground">Human input timeout (seconds)</span>
                        <Input
                          type="number"
                          min={1}
                          value={String(draft.engine_config.temporal?.human_input_resume_timeout_seconds ?? 0)}
                          onChange={(event) => updateDraftTemporalField("human_input_resume_timeout_seconds", event.target.value)}
                        />
                      </label>
                    </>
                  ) : null}
                </div>
              </div>

              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="border-b px-5 py-3">
                  <h2 className="text-sm font-semibold">Retry policy</h2>
                </div>
                <div className="space-y-4 px-5 py-4">
                  {hasMixedRetryPolicies ? (
                    <div className="rounded-xl border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                      Existing operation settings are mixed. Saving here will apply one workflow-wide retry policy to every operation.
                    </div>
                  ) : null}
                  <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
                    <label className="space-y-1 text-sm">
                      <span className="text-muted-foreground">Manual retry</span>
                      <select
                        className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                        value={sharedRetryPolicy.manual_retry_enabled ? "enabled" : "disabled"}
                        onChange={(event) =>
                          updateSharedRetryField("manual_retry_enabled", event.target.value === "enabled")
                        }
                      >
                        <option value="enabled">Enabled</option>
                        <option value="disabled">Disabled</option>
                      </select>
                    </label>
                    <label className="space-y-1 text-sm">
                      <span className="text-muted-foreground">Max attempts</span>
                      <Input
                        type="number"
                        min={1}
                        value={String(sharedRetryPolicy.max_attempts)}
                        onChange={(event) => updateSharedRetryField("max_attempts", event.target.value)}
                      />
                    </label>
                    <label className="space-y-1 text-sm">
                      <span className="text-muted-foreground">Initial interval (seconds)</span>
                      <Input
                        type="number"
                        min={0}
                        value={String(sharedRetryPolicy.initial_interval_seconds)}
                        onChange={(event) => updateSharedRetryField("initial_interval_seconds", event.target.value)}
                      />
                    </label>
                    <label className="space-y-1 text-sm">
                      <span className="text-muted-foreground">Max interval (seconds)</span>
                      <Input
                        type="number"
                        min={0}
                        value={String(sharedRetryPolicy.max_interval_seconds)}
                        onChange={(event) => updateSharedRetryField("max_interval_seconds", event.target.value)}
                      />
                    </label>
                    <label className="space-y-1 text-sm">
                      <span className="text-muted-foreground">Backoff coefficient</span>
                      <Input
                        type="number"
                        min={1}
                        step="0.1"
                        value={String(sharedRetryPolicy.backoff_coefficient)}
                        onChange={(event) => updateSharedRetryField("backoff_coefficient", event.target.value)}
                      />
                    </label>
                  </div>

                  <div className="space-y-2">
                    <p className="text-sm font-medium">Do not retry these failure categories</p>
                    <div className="grid gap-2 md:grid-cols-2">
                      {KNOWN_ERROR_CATEGORIES.map((category) => {
                        const checked = sharedRetryPolicy.non_retryable_error_categories.includes(category);
                        return (
                          <label key={category} className="flex items-center gap-2 rounded-xl border px-3 py-2 text-sm">
                            <input type="checkbox" checked={checked} onChange={() => toggleNonRetryableCategory(category)} />
                            <span>{category}</span>
                          </label>
                        );
                      })}
                    </div>
                  </div>

                  <div className="rounded-xl border bg-muted/20 px-3 py-2 text-sm text-muted-foreground">
                    {retryPolicySummary}
                  </div>
                </div>
              </div>

              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="border-b px-5 py-3">
                  <h2 className="text-sm font-semibold">Operations</h2>
                </div>
                <div className="border-b bg-muted/10 px-5 py-3 text-sm text-muted-foreground">
                  Operations define what this workflow can do. They inherit the workflow retry policy above unless a future workflow version explicitly changes that contract.
                </div>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Operation</TableHead>
                      <TableHead>Role</TableHead>
                      <TableHead>Notes</TableHead>
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
                        <TableCell className="text-sm text-muted-foreground">
                          {operation.status?.trim() || "Defined in workflow"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </div>

            <div className="space-y-4">
              <div className="overflow-hidden rounded-2xl border bg-background">
                <div className="border-b px-5 py-3">
                  <h2 className="text-sm font-semibold">Definition</h2>
                </div>
                <div className="space-y-4 px-5 py-4">
                  <div>
                    <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">State path</p>
                    <p className="mt-2 text-sm text-muted-foreground">{workflowType.lifecycle.state_path_kind || "—"}</p>
                  </div>
                  <div>
                    <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">States</p>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {workflowType.lifecycle.states.map((state) => (
                        <StatusBadge key={state.key} status={state.label} />
                      ))}
                    </div>
                  </div>
                  <div>
                    <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Transitions</p>
                    <div className="mt-2 space-y-2">
                      {workflowType.lifecycle.transitions.map((transition) => (
                        <div
                          key={`${transition.from_state}:${transition.to_state}:${transition.label}`}
                          className="rounded-xl border bg-muted/20 px-3 py-2 text-sm text-muted-foreground"
                        >
                          <div>{transition.label}</div>
                          <div className="mt-1 text-xs text-muted-foreground">
                            {transition.from_state} → {transition.to_state}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                  <div>
                    <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Conditional paths</p>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {workflowType.lifecycle.conditional_paths.length > 0 ? (
                        workflowType.lifecycle.conditional_paths.map((path) => (
                          <div key={path} className="rounded-full border bg-muted/20 px-3 py-1 text-xs text-muted-foreground">
                            {path}
                          </div>
                        ))
                      ) : (
                        <p className="text-sm text-muted-foreground">No conditional paths defined.</p>
                      )}
                    </div>
                  </div>
                  <Button onClick={() => void saveWorkflowType()} disabled={saving} size="sm">
                    {saving ? "Saving…" : "Save policy"}
                  </Button>
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
                          <TableCell className="whitespace-nowrap text-sm text-muted-foreground">
                            {formatTimestamp(execution.created_at)}
                          </TableCell>
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
