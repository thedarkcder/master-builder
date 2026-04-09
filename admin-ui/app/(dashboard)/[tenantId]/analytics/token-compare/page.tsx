"use client";

import { ChangeEvent } from "react";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { TokenStackedBarChart } from "@/components/charts";
import { compareRunsTokens, listProjects, listRuns, type ProjectRecord, type RunRecord, type TokenCompareRecord } from "@/lib/api";
import { useAuth } from "@/components/auth-provider";

type CompareFormState = {
  project_id: string;
  selected_issue_keys: string[];
  selected_run_ids: string[];
  alignBy: "turn_sequence" | "recorded_at";
};

function issueOptionsFromRuns(runs: RunRecord[]): string[] {
  return runs
    .map((run) => run.issue_key)
    .filter((issueKey): issueKey is string => Boolean(issueKey))
    .filter((value, index, self) => self.indexOf(value) === index)
    .sort();
}

function formatNumber(value: number): string {
  const normalized = Math.max(0, Math.floor(value));
  if (Math.abs(normalized) >= 1000000) {
    return `${(normalized / 1000000).toFixed(2)}m`;
  }
  if (Math.abs(normalized) >= 1000) {
    return `${(normalized / 1000).toFixed(1)}k`;
  }
  return String(normalized);
}

function IssueKeySelector(props: {
  issueOptions: string[];
  selectedIssues: string[];
  disabled: boolean;
  onChange: (next: string[]) => void;
}) {
  const { issueOptions, selectedIssues, disabled, onChange } = props;

  return (
    <label className="text-sm">
      <span className="mb-1 block">Issues (optional)</span>
      <p className="mb-1 text-xs text-muted-foreground">Hold Ctrl/Cmd to select multiple issues.</p>
      <select
        multiple
        size={6}
        className="h-28 w-full rounded-md border border-input bg-background px-3 py-1 text-sm"
        value={selectedIssues}
        onChange={(event) =>
          onChange(Array.from(event.currentTarget.selectedOptions).map((option) => option.value))
        }
        disabled={disabled}
      >
        {issueOptions.length === 0 ? (
          <option disabled value="">
            {disabled ? "Select a project first." : "No issues available for this project."}
          </option>
        ) : null}
        {issueOptions.map((issue) => (
          <option key={issue} value={issue}>
            {issue}
          </option>
        ))}
      </select>
    </label>
  );
}

export default function TenantTokenComparePage() {
  const params = useParams<{ tenantId: string }>();
  const tenantId = decodeURIComponent(params.tenantId);
  const { credentials, ready } = useAuth();
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [runOptions, setRunOptions] = useState<RunRecord[]>([]);
  const [form, setForm] = useState<CompareFormState>({
    project_id: "",
    selected_issue_keys: [],
    selected_run_ids: [],
    alignBy: "turn_sequence",
  });
  const [busy, setBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("Select a project and at least two runs to compare.");
  const [results, setResults] = useState<TokenCompareRecord | null>(null);

  const runIds = useMemo(() => form.selected_run_ids, [form.selected_run_ids]);
  const issueOptions = useMemo(() => issueOptionsFromRuns(runOptions), [runOptions]);
  const filteredRunOptions = useMemo(
    () =>
      form.selected_issue_keys.length === 0
        ? runOptions
        : runOptions.filter((run) => run.issue_key && form.selected_issue_keys.includes(run.issue_key)),
    [form.selected_issue_keys, runOptions]
  );
  const waterfallChartData = useMemo(
    () =>
      (results?.waterfall ?? []).map((step) => ({
        turn: `${step.turn_order}`,
        delta_input: step.delta_input,
        delta_uncached: step.uncached_delta,
        output_tokens: step.output_tokens,
      })),
    [results?.waterfall]
  );

  const setSelectedRuns = useCallback(
    (event: ChangeEvent<HTMLSelectElement>) => {
      const next = Array.from(event.target.selectedOptions).map((option) => option.value).slice(0, 20);
      setForm((current) => ({ ...current, selected_run_ids: next }));
    },
    []
  );

  const setSelectedIssues = useCallback((next: string[]) => {
    setForm((current) => ({
      ...current,
      selected_issue_keys: next,
      selected_run_ids: [],
    }));
  }, []);

  const compareRuns = useCallback(async () => {
    if (!credentials) {
      return;
    }
    if (!form.project_id) {
      setStatusLine("Select a project before comparing runs.");
      return;
    }
    if (runIds.length < 2) {
      setStatusLine("Please select at least two runs.");
      return;
    }
    setBusy(true);
    try {
      const payload = await compareRunsTokens(
        credentials,
        {
          run_ids: runIds,
          align_by: form.alignBy,
        },
        {
          tenantId,
          projectId: form.project_id,
        }
      );
      setResults(payload);
      setStatusLine(`Compared ${runIds.length} runs across ${payload.align_axis.length} turn buckets.`);
    } catch (error) {
      setResults(null);
      setStatusLine(`Compare failed: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }, [credentials, form.alignBy, form.project_id, runIds]);

  const loadProjects = useCallback(async () => {
    if (!credentials) {
      return;
    }
    try {
      const projectPayload = await listProjects(credentials, tenantId);
      setProjects(projectPayload);
      setForm((current) => {
        if (!projectPayload.length) {
          return current.project_id || current.selected_run_ids.length
            ? { ...current, project_id: "", selected_issue_keys: [], selected_run_ids: [] }
            : current;
        }
        if (current.project_id && projectPayload.some((project) => project.project_id === current.project_id)) {
          return current;
        }
        return {
          ...current,
          project_id: projectPayload[0]?.project_id ?? "",
          selected_issue_keys: [],
          selected_run_ids: [],
        };
      });
    } catch (error) {
      setStatusLine(`Failed to load projects: ${(error as Error).message}`);
    }
  }, [credentials, tenantId]);

  const loadRuns = useCallback(async () => {
    if (!credentials) {
      return;
    }
    if (!form.project_id) {
      setRunOptions([]);
      setForm((current) =>
        current.selected_issue_keys.length || current.selected_run_ids.length
          ? { ...current, selected_issue_keys: [], selected_run_ids: [] }
          : current
      );
      return;
    }
    try {
      const runPayload = await listRuns(credentials, {
        tenantId,
        projectId: form.project_id,
        limit: 200,
      });
      const orderedRuns = [...runPayload].sort((a, b) => b.created_at.localeCompare(a.created_at));
      setRunOptions(orderedRuns);
      const issueValues = issueOptionsFromRuns(orderedRuns);
      setForm((current) => {
        const selectedIssues = current.selected_issue_keys.filter((issue) => issueValues.includes(issue));
        const runSubset =
          selectedIssues.length === 0
            ? orderedRuns
            : orderedRuns.filter((run) => run.issue_key && selectedIssues.includes(run.issue_key));
        const filtered = current.selected_run_ids.filter((runId) => runSubset.some((run) => run.run_id === runId));
        return {
          ...current,
          selected_issue_keys: selectedIssues,
          selected_run_ids: filtered,
        };
      });
      if (orderedRuns.length === 0) {
        setStatusLine("No runs found for selected project.");
      }
    } catch (error) {
      setRunOptions([]);
      setStatusLine(`Failed to load runs: ${(error as Error).message}`);
    }
  }, [credentials, form.project_id, tenantId]);

  useEffect(() => {
    if (ready && credentials) {
      void loadProjects();
    }
  }, [ready, credentials, loadProjects]);

  useEffect(() => {
    if (ready && credentials) {
      void loadRuns();
    }
  }, [ready, credentials, loadRuns]);

  useEffect(() => {
    if (ready && credentials && form.project_id && runIds.length >= 2) {
      void compareRuns();
    }
  }, [ready, credentials, form.project_id, runIds, compareRuns]);

  return (
    <div className="space-y-6">
      <details className="group rounded-lg border bg-card" open>
        <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3 text-sm font-medium">
          <span>Filters</span>
          <span className="text-xs text-muted-foreground group-open:hidden">Show</span>
          <span className="text-xs text-muted-foreground hidden group-open:inline">Hide</span>
        </summary>
        <div className="border-t px-4 pb-4 pt-3 space-y-3">
        <div className="grid gap-2 md:grid-cols-3">
          <select
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
            value={form.project_id}
            onChange={(event) =>
              setForm((current) => ({
                ...current,
                project_id: event.target.value,
                selected_issue_keys: [],
                selected_run_ids: [],
              }))
            }
          >
            <option value="">Select project</option>
            {projects.map((project) => (
              <option key={project.project_id} value={project.project_id}>
                {project.name}
              </option>
            ))}
          </select>
          <IssueKeySelector
            issueOptions={issueOptions}
            selectedIssues={form.selected_issue_keys}
            disabled={!form.project_id || issueOptions.length === 0}
            onChange={setSelectedIssues}
          />
          <label className="flex items-center gap-2 text-sm">
            <span>Align by</span>
            <select
              className="h-9 rounded-md border border-input bg-background px-2"
              value={form.alignBy}
              onChange={(event) => setForm((current) => ({ ...current, alignBy: event.target.value as "turn_sequence" | "recorded_at" }))}
            >
              <option value="turn_sequence">Turn sequence</option>
              <option value="recorded_at">Recorded at</option>
            </select>
          </label>
        </div>
        <div className="space-y-2">
          <label className="text-sm font-medium">Select runs (max 20)</label>
          <select
            multiple
            size={10}
            className="h-40 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            value={form.selected_run_ids}
            onChange={setSelectedRuns}
            disabled={!form.project_id || filteredRunOptions.length === 0}
          >
            {filteredRunOptions.length === 0 ? (
              <option value="" disabled>
                {form.project_id
                  ? form.selected_issue_keys.length > 0
                    ? "No runs available for selected issues."
                    : "No runs available for this project."
                  : "Select a project first."}
              </option>
            ) : null}
            {filteredRunOptions.map((run) => (
              <option key={run.run_id} value={run.run_id}>
                {run.issue_key ? `${run.issue_key} · ` : ""}
                {run.run_id} · {run.status}
              </option>
            ))}
          </select>
          <p className="text-xs text-muted-foreground">
            Run list is limited to 200 recent runs for the selected project and active issue filters.
          </p>
          <p className="text-sm text-muted-foreground">Selected runs: {runIds.length} / 20</p>
          <p className="text-sm text-muted-foreground">Selected issues: {form.selected_issue_keys.length}</p>
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" onClick={() => void compareRuns()} disabled={busy || runIds.length < 2 || !form.project_id}>
            {busy ? "Comparing..." : "Compare"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setForm((current) => ({ ...current, selected_issue_keys: [], selected_run_ids: [] }));
              setResults(null);
              setStatusLine("Select a project and at least two runs to compare.");
            }}
            disabled={busy}
          >
            Clear
          </Button>
        </div>
        </div>
      </details>

      {!results ? (
          <p className="text-sm text-muted-foreground">{statusLine}</p>
        ) : (
          <>
            <div className="grid gap-3 md:grid-cols-2">
              {results.runs.map((run) => (
                <div key={run.run_id} className="overflow-hidden rounded-2xl border bg-background">
                  <div className="p-4">
                    <p className="mb-1 text-xs font-semibold break-all text-muted-foreground">Run</p>
                    <p className="mb-2 font-medium text-sm break-all">{run.run_id}</p>
                    <p className="mb-3 text-xs text-muted-foreground">{run.issue_key} · {run.status}</p>
                    <div className="grid grid-cols-2 gap-2 text-xs">
                      {[
                        ["Input", formatNumber(run.totals.input)],
                        ["Uncached", formatNumber(run.totals.uncached_input)],
                        ["Output", formatNumber(run.totals.output)],
                        ["Total I/O", formatNumber(run.totals.total_io)],
                        ["Cache Ratio", `${(run.totals.cache_ratio * 100).toFixed(1)}%`]
                      ].map(([label, value]) => (
                        <div key={label} className="rounded-xl border px-4 py-3">
                          <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">{label}</p>
                          <p className="font-semibold">{value}</p>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              ))}
            </div>
            <div className="overflow-hidden rounded-2xl border bg-background">
              <div className="px-6 pt-6 pb-3">
                <h2 className="text-base font-semibold">Per-stage Totals</h2>
              </div>
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Run</TableHead>
                      <TableHead>Stage</TableHead>
                      <TableHead className="text-right">Input</TableHead>
                      <TableHead className="text-right">Uncached</TableHead>
                      <TableHead className="text-right">Output</TableHead>
                      <TableHead className="text-right">Total I/O</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {results.runs.flatMap((run) =>
                      run.stage_totals.map((stage) => (
                        <TableRow key={`${run.run_id}-${stage.stage}`}>
                          <TableCell>{run.run_id}</TableCell>
                          <TableCell>{stage.stage.toUpperCase()}</TableCell>
                          <TableCell className="text-right">{formatNumber(stage.input)}</TableCell>
                          <TableCell className="text-right">{formatNumber(stage.uncached_input)}</TableCell>
                          <TableCell className="text-right">{formatNumber(stage.output)}</TableCell>
                          <TableCell className="text-right">{formatNumber(stage.total_io)}</TableCell>
                        </TableRow>
                      ))
                    )}
                    {results.runs.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={6} className="text-center text-muted-foreground">
                          No stage totals available.
                        </TableCell>
                      </TableRow>
                    ) : null}
                  </TableBody>
                </Table>
              </div>
            </div>
            <div className="overflow-hidden rounded-2xl border bg-background">
              <div className="px-6 pt-6 pb-3">
                <h2 className="text-base font-semibold">Delta Waterfall</h2>
              </div>
              <div className="p-6 pt-0">
              {results.waterfall.length === 0 ? (
                <p className="text-sm text-muted-foreground">No turn data in selected runs.</p>
              ) : (
                <div className="space-y-3">
                  <TokenStackedBarChart
                    data={waterfallChartData}
                    xAxisKey="turn"
                    bars={[
                      { key: "delta_input", label: "Δ Input", color: "#0ea5e9", stackId: "delta" },
                      { key: "delta_uncached", label: "Δ Uncached", color: "#f59e0b", stackId: "delta" },
                      { key: "output_tokens", label: "Output", color: "#ef4444", stackId: "output" },
                    ]}
                  />
                  <div className="overflow-x-auto">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Run</TableHead>
                          <TableHead>Turn</TableHead>
                          <TableHead>Stage</TableHead>
                          <TableHead>Attempt</TableHead>
                          <TableHead className="text-right">Δ Input</TableHead>
                          <TableHead className="text-right">Δ Uncached</TableHead>
                          <TableHead className="text-right">Output</TableHead>
                          <TableHead>Reasons</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {results.waterfall.map((step) => (
                          <TableRow key={`${step.run_id}-${step.turn_id}-${step.turn_order}`}>
                            <TableCell>{step.run_id}</TableCell>
                            <TableCell>{step.turn_order}</TableCell>
                            <TableCell>{step.stage}</TableCell>
                            <TableCell>{step.attempt === null ? "-" : step.attempt}</TableCell>
                            <TableCell className="text-right">{formatNumber(step.delta_input)}</TableCell>
                            <TableCell className="text-right">{formatNumber(step.uncached_delta)}</TableCell>
                            <TableCell className="text-right">{formatNumber(step.output_tokens)}</TableCell>
                            <TableCell>{step.delta_reason.join(", ") || "none"}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                </div>
              )}
              </div>
            </div>
          </>
        )}
        {statusLine ? <p className="text-sm text-muted-foreground">{statusLine}</p> : null}
      </div>
  );
}
