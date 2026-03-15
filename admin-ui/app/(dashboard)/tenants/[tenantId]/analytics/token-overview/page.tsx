"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { TokenLineChart, TokenScatterChart, TokenStackedBarChart, formatMetricValue } from "@/components/charts";
import { useAuth } from "@/components/auth-provider";
import {
  getTokenOverview,
  listProjects,
  listRuns,
  type ProjectRecord,
  type RunRecord,
  type TokenOverviewRecord,
  type TokenOverviewSeriesPoint
} from "@/lib/api";

type TokenOverviewStatus = {
  project_id: string;
  issue_keys: string[];
  run_status: string;
  from: string;
  to: string;
  stage: string;
  attempt: string;
  model: string;
  only_retried: boolean;
  only_with_test_stage: boolean;
};

function statusBadge(_status: string) {
  return null;
}

function toIsoDate(value: string): string {
  if (!value) {
    return "";
  }
  return new Date(`${value}T00:00:00.000Z`).toISOString();
}

function numberToDate(value: string): string {
  return new Date(`${value}T00:00:00.000Z`).toLocaleDateString();
}

function attemptOptions() {
  return Array.from({ length: 20 }, (_, index) => String(index + 1));
}

function statusOptionsFromRuns(runs: RunRecord[]): string[] {
  const statuses = runs
    .map((run) => run.status)
    .filter((status) => Boolean(status))
    .filter((value, index, self) => self.indexOf(value) === index)
    .sort();
  return statuses;
}

function issueOptionsFromRuns(runs: RunRecord[]): string[] {
  const issueKeys = runs
    .map((run) => run.issue_key)
    .filter((issueKey): issueKey is string => Boolean(issueKey))
    .filter((value, index, self) => self.indexOf(value) === index)
    .sort();
  return issueKeys;
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

export default function TenantTokenOverviewPage() {
  const params = useParams<{ tenantId: string }>();
  const { credentials, ready } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);
  const [filters, setFilters] = useState<TokenOverviewStatus>({
    project_id: "",
    issue_keys: [],
    run_status: "",
    from: "",
    to: "",
    stage: "",
    attempt: "",
    model: "",
    only_retried: false,
    only_with_test_stage: false,
  });
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [runsForFilters, setRunsForFilters] = useState<RunRecord[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [busy, setBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("Load token overview to inspect efficiency trends.");
  const [overview, setOverview] = useState<TokenOverviewRecord | null>(null);

  const issueOptions = useMemo(() => issueOptionsFromRuns(runsForFilters), [runsForFilters]);
  const runStatusOptions = useMemo(() => statusOptionsFromRuns(runsForFilters), [runsForFilters]);

  const applyFilters = useCallback(
    (updater: (prev: TokenOverviewStatus) => TokenOverviewStatus) => {
      setFilters((prev) => updater(prev));
      setPage(1);
    },
    []
  );

  const loadOverview = useCallback(async () => {
    if (!credentials) {
      return;
    }
    if (!filters.project_id) {
      setOverview(null);
      setStatusLine("Select a project to load token overview.");
      return;
    }
    setBusy(true);
    try {
      const payload = await getTokenOverview(credentials, {
        tenant_id: tenantId,
        project_id: filters.project_id,
        issue_key: filters.issue_keys.length > 0 ? filters.issue_keys.join(",") : undefined,
        run_status: filters.run_status || undefined,
        stage: filters.stage || undefined,
        attempt: Number(filters.attempt) > 0 ? Number(filters.attempt) : undefined,
        model: filters.model.trim() || undefined,
        start_date: toIsoDate(filters.from),
        end_date: toIsoDate(filters.to),
        only_retried: filters.only_retried,
        only_with_test_stage: filters.only_with_test_stage,
        page,
        page_size: pageSize,
      });
      setOverview(payload);
      setStatusLine(`Loaded ${payload.top_costly_runs.length} costliest runs for the selected filters.`);
    } catch (error) {
      setOverview(null);
      setStatusLine(`Failed to load overview: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }, [credentials, filters.attempt, filters.from, filters.to, filters.issue_keys, filters.model, filters.only_retried, filters.only_with_test_stage, filters.project_id, filters.run_status, filters.stage, page, pageSize, tenantId]);

  const loadProjects = useCallback(async () => {
    if (!credentials) {
      return;
    }
    try {
      const projectPayload = await listProjects(credentials, tenantId);
      setProjects(projectPayload);
      setFilters((current) => {
        if (!projectPayload.length) {
          return current.project_id ? { ...current, project_id: "", issue_keys: [], run_status: "" } : current;
        }
        if (current.project_id && projectPayload.some((project) => project.project_id === current.project_id)) {
          return current;
        }
        return { ...current, project_id: projectPayload[0]?.project_id ?? "", issue_keys: [], run_status: "" };
      });
    } catch (error) {
      setStatusLine(`Failed to load projects: ${(error as Error).message}`);
    }
  }, [credentials, tenantId]);

  const loadRunsForFilters = useCallback(
    async (projectId: string) => {
      if (!credentials) {
        return;
      }
      if (!projectId) {
        setRunsForFilters([]);
        setFilters((current) => (current.issue_keys.length || current.run_status ? { ...current, issue_keys: [], run_status: "" } : current));
        return;
      }
      try {
        const runPayload = await listRuns(credentials, {
          tenantId,
          projectId,
          limit: 200
        });
        const orderedRuns = [...runPayload].sort((a, b) => b.created_at.localeCompare(a.created_at));
        setRunsForFilters(orderedRuns);
        setFilters((current) => {
          const issueOptions = issueOptionsFromRuns(orderedRuns);
          const nextIssues = current.issue_keys.filter((issue) => issueOptions.includes(issue));
          const runStatuses = statusOptionsFromRuns(orderedRuns);
          const nextStatus = current.run_status && runStatuses.includes(current.run_status) ? current.run_status : "";
          return {
            ...current,
            issue_keys: nextIssues,
            run_status: nextStatus,
          };
        });
      } catch (error) {
        setRunsForFilters([]);
        setStatusLine(`Failed to load runs for filters: ${(error as Error).message}`);
      }
    },
    [credentials, tenantId]
  );

  useEffect(() => {
    if (ready && credentials) {
      void loadProjects();
    }
  }, [ready, credentials, loadProjects]);

  useEffect(() => {
    if (ready && credentials) {
      void loadRunsForFilters(filters.project_id);
    }
  }, [ready, credentials, filters.project_id, loadRunsForFilters]);

  useEffect(() => {
    if (ready && credentials) {
      void loadOverview();
    }
  }, [ready, credentials, loadOverview]);

  const seriesByDay = useMemo<TokenOverviewSeriesPoint[]>(() => overview?.series_by_day ?? [], [overview]);
  const trendSeries = useMemo(
    () =>
      seriesByDay.map((day) => ({
        day: numberToDate(day.day),
        total_input: day.total_input,
        total_uncached_input: day.total_uncached_input,
        total_output: day.total_output,
        total_io: day.total_io,
        delta_input: day.delta_input,
        delta_uncached: day.delta_uncached,
        delta_output: day.delta_output,
        avg_runtime_ms: day.avg_runtime_ms,
        run_count: day.run_count,
      })),
    [seriesByDay]
  );

  const stackedBarData = useMemo(
    () =>
      trendSeries.map((point) => ({
        day: point.day,
        cached_input: Math.max(0, point.total_input - point.total_uncached_input),
        uncached_input: point.total_uncached_input,
        output: point.total_output,
      })),
    [trendSeries]
  );

  return (
    <div className="space-y-6">
      {/* Collapsible filter panel */}
      <details className="group rounded-lg border bg-card" open>
        <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3 text-sm font-medium">
          <span>Filters</span>
          <span className="text-xs text-muted-foreground group-open:hidden">Show</span>
          <span className="text-xs text-muted-foreground hidden group-open:inline">Hide</span>
        </summary>
        <div className="border-t px-4 pb-4 pt-3 space-y-3">
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <select
              className="h-9 rounded-md border border-input bg-background px-3 text-sm"
              value={filters.project_id}
              onChange={(event) =>
                applyFilters((prev) => ({ ...prev, project_id: event.target.value, issue_keys: [], run_status: "" }))
              }
            >
              <option value="">Select project</option>
              {projects.map((project) => (
                <option key={project.project_id} value={project.project_id}>{project.name}</option>
              ))}
            </select>
            <IssueKeySelector
              issueOptions={issueOptions}
              selectedIssues={filters.issue_keys}
              disabled={!filters.project_id}
              onChange={(next) => applyFilters((prev) => ({ ...prev, issue_keys: next }))}
            />
            <select
              className="h-9 rounded-md border border-input bg-background px-3 text-sm"
              value={filters.run_status}
              disabled={!filters.project_id}
              onChange={(event) => applyFilters((prev) => ({ ...prev, run_status: event.target.value }))}
            >
              <option value="">Any status</option>
              {runStatusOptions.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            <select
              className="h-9 rounded-md border border-input bg-background px-3 text-sm"
              value={filters.stage}
              onChange={(event) => applyFilters((prev) => ({ ...prev, stage: event.target.value }))}
            >
              <option value="">Any stage</option>
              <option value="pm">pm</option>
              <option value="dev">dev</option>
              <option value="test">test</option>
              <option value="review">review</option>
            </select>
            <Input type="date" value={filters.from} onChange={(event) => applyFilters((prev) => ({ ...prev, from: event.target.value }))} />
            <Input type="date" value={filters.to} onChange={(event) => applyFilters((prev) => ({ ...prev, to: event.target.value }))} />
            <select
              className="h-9 rounded-md border border-input bg-background px-3 text-sm"
              value={filters.attempt}
              onChange={(event) => applyFilters((prev) => ({ ...prev, attempt: event.target.value }))}
            >
              <option value="">Any attempt</option>
              {attemptOptions().map((a) => <option key={a} value={a}>{a}</option>)}
            </select>
            <Input value={filters.model} placeholder="Model (optional)" onChange={(event) => applyFilters((prev) => ({ ...prev, model: event.target.value }))} />
          </div>
          <div className="flex flex-wrap items-center gap-4 text-sm">
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" checked={filters.only_retried} onChange={(e) => applyFilters((prev) => ({ ...prev, only_retried: e.target.checked }))} />
              Only retried runs
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" checked={filters.only_with_test_stage} onChange={(e) => applyFilters((prev) => ({ ...prev, only_with_test_stage: e.target.checked }))} />
              Only runs with test stage
            </label>
          </div>
          <div className="flex items-center gap-2">
            <Button size="sm" onClick={() => void loadOverview()} disabled={busy}>
              {busy ? "Loading..." : "Apply"}
            </Button>
            <Button variant="outline" size="sm" onClick={() => { setPage(1); setFilters({ project_id: "", issue_keys: [], run_status: "", from: "", to: "", stage: "", attempt: "", model: "", only_retried: false, only_with_test_stage: false }); }} disabled={busy}>
              Clear
            </Button>
            <div className="ml-auto flex items-center gap-2 text-sm">
              Page size
              <select className="h-8 rounded-md border border-input bg-background px-2 text-xs" value={String(pageSize)} onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1); }}>
                <option value="10">10</option>
                <option value="20">20</option>
                <option value="50">50</option>
              </select>
            </div>
          </div>
        </div>
      </details>

      {/* KPI cards */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardContent className="p-4">
            <p className="text-xs uppercase text-muted-foreground">Total Input</p>
            <p className="mt-1 text-2xl font-bold">{formatMetricValue(overview?.kpis.total_input ?? 0)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs uppercase text-muted-foreground">Uncached Input</p>
            <p className="mt-1 text-2xl font-bold">{formatMetricValue(overview?.kpis.total_uncached_input ?? 0)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs uppercase text-muted-foreground">Output</p>
            <p className="mt-1 text-2xl font-bold">{formatMetricValue(overview?.kpis.total_output ?? 0)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs uppercase text-muted-foreground">Cache Ratio</p>
            <p className="mt-1 text-2xl font-bold">{((overview?.kpis.cache_ratio ?? 0) * 100).toFixed(1)}%</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs uppercase text-muted-foreground">Avg I/O / Run</p>
            <p className="mt-1 text-2xl font-bold">{formatMetricValue(overview?.kpis.avg_io_per_run ?? 0)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs uppercase text-muted-foreground">P95 I/O / Run</p>
            <p className="mt-1 text-2xl font-bold">{formatMetricValue(overview?.kpis.p95_io_per_run ?? 0)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs uppercase text-muted-foreground">Retest Waste Score</p>
            <p className="mt-1 text-2xl font-bold">{((overview?.kpis.retest_waste_score ?? 0) * 100).toFixed(1)}%</p>
          </CardContent>
        </Card>
      </div>
        {!overview ? (
          <p className="text-sm text-muted-foreground">{statusLine}</p>
        ) : (
          <>
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Totals and deltas by day</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <TokenLineChart
                  data={trendSeries}
                  xAxisKey="day"
                  lines={[
                    { key: "total_input", label: "Input", color: "#0ea5e9" },
                    { key: "total_output", label: "Output", color: "#ef4444" },
                    { key: "delta_input", label: "Δ Input", color: "#f59e0b" },
                  ]}
                />
                <TokenStackedBarChart
                  data={stackedBarData}
                  xAxisKey="day"
                  bars={[
                    { key: "cached_input", label: "Cached input", color: "#0ea5e9", stackId: "tokens" },
                    { key: "uncached_input", label: "Uncached input", color: "#f97316", stackId: "tokens" },
                    { key: "output", label: "Output", color: "#ef4444", stackId: "output" },
                  ]}
                />
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Runtime vs delta input</CardTitle>
                <CardDescription>Scatter of day-level input growth against average turn runtime.</CardDescription>
              </CardHeader>
              <CardContent>
                <TokenScatterChart
                  data={trendSeries.filter(
                    (point) =>
                      point.avg_runtime_ms > 0 &&
                      (point.delta_input > 0 || point.delta_uncached > 0 || point.delta_output > 0)
                  )}
                  xAxisKey="avg_runtime_ms"
                  yAxisKey="delta_input"
                  pointLabel="Token Δ"
                />
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-sm">Top Costly Runs</CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Run</TableHead>
                      <TableHead>Issue</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead className="text-right">Input</TableHead>
                      <TableHead className="text-right">Uncached</TableHead>
                      <TableHead className="text-right">Output</TableHead>
                      <TableHead className="text-right">Total I/O</TableHead>
                      <TableHead className="text-right">Delta I/O</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {overview.top_costly_runs.map((run) => (
                      <TableRow key={run.run_id}>
                        <TableCell>
                          <Link
                            className="text-primary hover:underline"
                            href={`/tenants/${encodeURIComponent(tenantId)}/runs/${encodeURIComponent(run.run_id)}`}
                          >
                            {run.run_id}
                          </Link>
                        </TableCell>
                        <TableCell>{run.issue_key}</TableCell>
                        <TableCell><StatusBadge status={run.status} /></TableCell>
                        <TableCell className="text-right">{formatMetricValue(run.input)}</TableCell>
                        <TableCell className="text-right">{formatMetricValue(run.uncached_input)}</TableCell>
                        <TableCell className="text-right">{formatMetricValue(run.output)}</TableCell>
                        <TableCell className="text-right">{formatMetricValue(run.total_io)}</TableCell>
                        <TableCell className="text-right">{formatMetricValue(run.delta_total_io)}</TableCell>
                      </TableRow>
                    ))}
                    {overview.top_costly_runs.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={8} className="text-center text-muted-foreground">
                          No runs matched the selected filters.
                        </TableCell>
                      </TableRow>
                    ) : null}
                  </TableBody>
                </Table>
              </div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-sm">Alerts</CardTitle>
              </CardHeader>
              <CardContent>
                {overview.alerts.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No alerts for current filters.</p>
                ) : (
                  <ul className="space-y-1.5">
                    {overview.alerts.map((alert, idx) => (
                      <li key={`${alert.rule}-${alert.run_id}-${idx}`} className="rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm">
                        <p className="font-medium">
                          <span className="text-muted-foreground">[{alert.rule}]</span> {alert.message}
                        </p>
                        <p className="text-xs text-muted-foreground">
                          run {alert.run_id}
                          {alert.turn_id ? ` · turn ${alert.turn_id}` : ""}
                          {alert.stage ? ` · ${alert.stage}` : ""}
                          {alert.attempt !== null ? ` · attempt ${alert.attempt}` : ""}
                        </p>
                      </li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>
          </>
        )}
        <div className="flex items-center justify-between gap-2 text-sm text-muted-foreground">
          <p>{statusLine}</p>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => setPage((c) => Math.max(1, c - 1))} disabled={busy || page <= 1}>← Prev</Button>
            <span>Page {page}</span>
            <Button variant="outline" size="sm" onClick={() => setPage((c) => c + 1)} disabled={busy || !overview || overview.top_costly_runs.length < pageSize}>Next →</Button>
          </div>
        </div>
      </div>
  );
}
