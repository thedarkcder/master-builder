"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { TokenRadarChart, TokenScatterChart, TokenStackedBarChart, formatMetricValue } from "@/components/charts";
import { useAuth } from "@/components/auth-provider";
import {
  getTokenStageDiagnosticsCompare,
  getTokenStageDiagnostics,
  listProjects,
  listRuns,
  type ProjectRecord,
  type RunRecord,
  type TokenStageDiagnosticsCompareRecord,
  type TokenStageDiagnosticsRecord
} from "@/lib/api";

type FilterState = {
  project_id: string;
  issue_keys: string[];
  run_status: string;
  stage: string;
  attempt: string;
  model: string;
  from: string;
  to: string;
  only_retried: boolean;
  only_with_test_stage: boolean;
};

function toIsoDate(value: string): string {
  if (!value) {
    return "";
  }
  return new Date(`${value}T00:00:00.000Z`).toISOString();
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

function stageColor(stage: string): string {
  if (stage === "pm") {
    return "#0ea5e9";
  }
  if (stage === "dev") {
    return "#22c55e";
  }
  if (stage === "test") {
    return "#f59e0b";
  }
  return "#ef4444";
}

function attemptOptions() {
  return Array.from({ length: 20 }, (_, index) => String(index + 1));
}

function statusOptionsFromRuns(runs: RunRecord[]): string[] {
  return runs
    .map((run) => run.status)
    .filter((status) => Boolean(status))
    .filter((value, index, self) => self.indexOf(value) === index)
    .sort();
}

function issueOptionsFromRuns(runs: RunRecord[]): string[] {
  return runs
    .map((run) => run.issue_key)
    .filter((issueKey): issueKey is string => Boolean(issueKey))
    .filter((value, index, self) => self.indexOf(value) === index)
    .sort();
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

export default function TenantStageDiagnosticsPage() {
  const params = useParams<{ tenantId: string }>();
  const tenantId = decodeURIComponent(params.tenantId);
  const { credentials, ready } = useAuth();
  const [filters, setFilters] = useState<FilterState>({
    project_id: "",
    issue_keys: [],
    run_status: "",
    stage: "",
    attempt: "",
    model: "",
    from: "",
    to: "",
    only_retried: false,
    only_with_test_stage: false,
  });
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [runsForFilters, setRunsForFilters] = useState<RunRecord[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [busy, setBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("Load diagnostics to inspect stage inefficiencies.");
  const [results, setResults] = useState<TokenStageDiagnosticsRecord | null>(null);
  const [compareProjectIds, setCompareProjectIds] = useState<string[]>([]);
  const [compareMetric, setCompareMetric] = useState<"avg_delta" | "avg_uncached_delta" | "retry_impact_index" | "run_count">("avg_delta");
  const [normalizeCompare, setNormalizeCompare] = useState(true);
  const [compareViewMode, setCompareViewMode] = useState<"overlay" | "small_multiples">("overlay");
  const [compareBusy, setCompareBusy] = useState(false);
  const [compareStatusLine, setCompareStatusLine] = useState("Select at least two projects to compare stage profiles.");
  const [compareResults, setCompareResults] = useState<TokenStageDiagnosticsCompareRecord | null>(null);

  const issueOptions = useMemo(() => issueOptionsFromRuns(runsForFilters), [runsForFilters]);
  const runStatusOptions = useMemo(() => statusOptionsFromRuns(runsForFilters), [runsForFilters]);
  const projectNameLookup = useMemo(
    () => Object.fromEntries(projects.map((project) => [project.project_id, project.name])),
    [projects]
  );

  const load = useCallback(async () => {
    if (!credentials) {
      return;
    }
    if (!filters.project_id) {
      setResults(null);
      setStatusLine("Select a project to load stage diagnostics.");
      return;
    }
    setBusy(true);
    try {
      const payload = await getTokenStageDiagnostics(credentials, {
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
      setResults(payload);
      setStatusLine(`Loaded ${payload.stages.length} stages and ${payload.heavy_commands.length} heavy command signatures.`);
    } catch (error) {
      setResults(null);
      setStatusLine(`Failed to load diagnostics: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }, [
    credentials,
    filters.attempt,
    filters.model,
    filters.from,
    filters.to,
    filters.issue_keys,
    filters.only_retried,
    filters.only_with_test_stage,
    filters.project_id,
    filters.run_status,
    filters.stage,
    page,
    pageSize,
    tenantId,
  ]);

  const loadProjects = useCallback(async () => {
    if (!credentials) {
      return;
    }
    try {
      const projectPayload = await listProjects(credentials, tenantId);
      setProjects(projectPayload);
      setFilters((current) => {
        if (!projectPayload.length) {
          return current.project_id || current.issue_keys.length || current.run_status
            ? { ...current, project_id: "", issue_keys: [], run_status: "" }
            : current;
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
        setFilters((current) =>
          current.issue_keys.length || current.run_status ? { ...current, issue_keys: [], run_status: "" } : current
        );
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
        const issueOptions = issueOptionsFromRuns(orderedRuns);
        const statusOptions = statusOptionsFromRuns(orderedRuns);
        setFilters((current) => {
          const nextIssues = current.issue_keys.filter((issue) => issueOptions.includes(issue));
          return {
            ...current,
            issue_keys: nextIssues,
            run_status: statusOptions.includes(current.run_status) ? current.run_status : "",
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
      void load();
    }
  }, [ready, credentials, load]);

  const loadCompare = useCallback(async () => {
    if (!credentials) {
      return;
    }
    if (compareProjectIds.length < 2) {
      setCompareResults(null);
      setCompareStatusLine("Select at least two projects to compare stage profiles.");
      return;
    }
    setCompareBusy(true);
    try {
      const payload = await getTokenStageDiagnosticsCompare(credentials, {
        tenant_id: tenantId,
        project_ids: compareProjectIds,
        issue_key: filters.issue_keys.length > 0 ? filters.issue_keys.join(",") : undefined,
        run_status: filters.run_status || undefined,
        stage: filters.stage || undefined,
        attempt: Number(filters.attempt) > 0 ? Number(filters.attempt) : undefined,
        model: filters.model.trim() || undefined,
        start_date: toIsoDate(filters.from),
        end_date: toIsoDate(filters.to),
        only_retried: filters.only_retried,
        only_with_test_stage: filters.only_with_test_stage,
      });
      setCompareResults(payload);
      setCompareStatusLine(`Compared ${payload.projects.length} projects across stage metrics.`);
    } catch (error) {
      setCompareResults(null);
      setCompareStatusLine(`Cross-project compare failed: ${(error as Error).message}`);
    } finally {
      setCompareBusy(false);
    }
  }, [
    compareProjectIds,
    credentials,
    filters.attempt,
    filters.from,
    filters.issue_keys,
    filters.model,
    filters.only_retried,
    filters.only_with_test_stage,
    filters.run_status,
    filters.stage,
    filters.to,
    tenantId,
  ]);

  useEffect(() => {
    if (ready && credentials) {
      void loadCompare();
    }
  }, [ready, credentials, loadCompare]);

  const scatterData = useMemo(
    () =>
      (results?.scatter_points ?? [])
        .filter((point) => point.runtime_ms !== null && point.runtime_ms > 0)
        .map((point) => ({
          x_runtime_ms: point.runtime_ms,
          y_token_delta: point.token_delta,
        })),
    [results?.scatter_points]
  );
  const radarData = useMemo(() => {
    const stageAxis = ["pm", "dev", "test", "review"];
    const projectsPayload = compareResults?.projects ?? [];
    const rows = stageAxis.map((stageName) => {
      const row: Record<string, string | number> = { stage: stageName.toUpperCase() };
      const values: number[] = [];
      for (const project of projectsPayload) {
        const stageEntry = project.stages.find((stage) => stage.stage === stageName);
        const value = stageEntry ? Number(stageEntry[compareMetric] ?? 0) : 0;
        row[project.project_id] = value;
        values.push(value);
      }
      if (normalizeCompare && values.length > 0) {
        const maxValue = Math.max(...values, 0);
        if (maxValue > 0) {
          for (const project of projectsPayload) {
            row[project.project_id] = Number(row[project.project_id] ?? 0) / maxValue;
          }
        }
      }
      return row;
    });
    return rows;
  }, [compareMetric, compareResults?.projects, normalizeCompare]);
  const radarSeries = useMemo(
    () =>
      (compareResults?.projects ?? []).map((project, index) => ({
        key: project.project_id,
        label: project.project_name || projectNameLookup[project.project_id] || project.project_id,
        color: ["#0ea5e9", "#22c55e", "#f59e0b", "#ef4444", "#8b5cf6", "#14b8a6"][index % 6],
      })),
    [compareResults?.projects, projectNameLookup]
  );
  const projectRadarRows = useMemo(() => {
    const stageAxis = ["pm", "dev", "test", "review"];
    return (compareResults?.projects ?? []).map((project) => {
      const baseRows = stageAxis.map((stageName) => {
        const stageEntry = project.stages.find((stage) => stage.stage === stageName);
        return {
          stage: stageName.toUpperCase(),
          value: stageEntry ? Number(stageEntry[compareMetric] ?? 0) : 0,
        };
      });
      if (!normalizeCompare) {
        return {
          project_id: project.project_id,
          project_name: project.project_name || projectNameLookup[project.project_id] || project.project_id,
          rows: baseRows,
        };
      }
      const maxValue = Math.max(...baseRows.map((row) => row.value), 0);
      const normalizedRows = maxValue > 0 ? baseRows.map((row) => ({ ...row, value: row.value / maxValue })) : baseRows;
      return {
        project_id: project.project_id,
        project_name: project.project_name || projectNameLookup[project.project_id] || project.project_id,
        rows: normalizedRows,
      };
    });
  }, [compareMetric, compareResults?.projects, normalizeCompare, projectNameLookup]);
  const issueStageChartData = useMemo(() => {
    const byIssue = new Map<string, { issue_key: string; pm: number; dev: number; test: number; review: number }>();
    for (const row of results?.issue_stage_totals ?? []) {
      const issue = byIssue.get(row.issue_key) ?? { issue_key: row.issue_key, pm: 0, dev: 0, test: 0, review: 0 };
      if (row.stage === "pm") issue.pm += row.total_io;
      if (row.stage === "dev") issue.dev += row.total_io;
      if (row.stage === "test") issue.test += row.total_io;
      if (row.stage === "review") issue.review += row.total_io;
      byIssue.set(row.issue_key, issue);
    }
    return Array.from(byIssue.values()).sort(
      (a, b) => (b.pm + b.dev + b.test + b.review) - (a.pm + a.dev + a.test + a.review)
    );
  }, [results?.issue_stage_totals]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Stage Diagnostics</CardTitle>
        <CardDescription>
          Drill into stage-level efficiency, retry impact, and command signatures correlated with token spikes.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-2 md:grid-cols-4">
          <select
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
            value={filters.project_id}
            onChange={(event) =>
              setFilters((current) => ({
                ...current,
                project_id: event.target.value,
                issue_keys: [],
                run_status: "",
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
            selectedIssues={filters.issue_keys}
            disabled={!filters.project_id || issueOptions.length === 0}
            onChange={(next) =>
              setFilters((current) => ({
                ...current,
                issue_keys: next,
              }))
            }
          />
          <select
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
            value={filters.run_status}
            disabled={!filters.project_id}
            onChange={(event) => setFilters((current) => ({ ...current, run_status: event.target.value }))}
          >
            <option value="">Any status</option>
            {runStatusOptions.map((status) => (
              <option key={status} value={status}>
                {status}
              </option>
            ))}
          </select>
          <select
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
            value={filters.stage}
            onChange={(event) => setFilters((current) => ({ ...current, stage: event.target.value }))}
          >
            <option value="">Any stage</option>
            <option value="pm">pm</option>
            <option value="dev">dev</option>
            <option value="test">test</option>
            <option value="review">review</option>
          </select>
          <select
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
            value={filters.attempt}
            onChange={(event) => setFilters((current) => ({ ...current, attempt: event.target.value }))}
          >
            <option value="">Any attempt</option>
            {attemptOptions().map((attempt) => (
              <option key={attempt} value={attempt}>
                {attempt}
              </option>
            ))}
          </select>
          <Input
            value={filters.model}
            placeholder="Model (optional)"
            onChange={(event) => setFilters((current) => ({ ...current, model: event.target.value }))}
          />
          <Input
            type="date"
            value={filters.from}
            onChange={(event) => setFilters((current) => ({ ...current, from: event.target.value }))}
          />
          <Input
            type="date"
            value={filters.to}
            onChange={(event) => setFilters((current) => ({ ...current, to: event.target.value }))}
          />
          <div className="flex items-center gap-4 text-sm">
            <label className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={filters.only_retried}
                onChange={(event) => setFilters((current) => ({ ...current, only_retried: event.target.checked }))}
              />
              Only retried runs
            </label>
            <label className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={filters.only_with_test_stage}
                onChange={(event) =>
                  setFilters((current) => ({ ...current, only_with_test_stage: event.target.checked }))
                }
              />
              Only with test stage
            </label>
          </div>
        </div>
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Cross-project stage compare</CardTitle>
            <CardDescription>Radar profile for stage metrics across selected projects.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid gap-2 md:grid-cols-3">
              <label className="text-sm">
                <span className="mb-1 block">Projects (2+)</span>
                <select
                  multiple
                  size={6}
                  className="h-28 w-full rounded-md border border-input bg-background px-3 py-1 text-sm"
                  value={compareProjectIds}
                  onChange={(event) =>
                    setCompareProjectIds(Array.from(event.currentTarget.selectedOptions).map((option) => option.value))
                  }
                >
                  {projects.map((project) => (
                    <option key={project.project_id} value={project.project_id}>
                      {project.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="text-sm">
                <span className="mb-1 block">Metric</span>
                <select
                  className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                  value={compareMetric}
                  onChange={(event) =>
                    setCompareMetric(event.target.value as "avg_delta" | "avg_uncached_delta" | "retry_impact_index" | "run_count")
                  }
                >
                  <option value="avg_delta">Avg Delta</option>
                  <option value="avg_uncached_delta">Avg Uncached Delta</option>
                  <option value="retry_impact_index">Retry Impact Index</option>
                  <option value="run_count">Run Count</option>
                </select>
              </label>
              <div className="flex items-center gap-3 text-sm">
                <label className="flex items-center gap-1">
                  <span>View</span>
                  <select
                    className="h-9 rounded-md border border-input bg-background px-2"
                    value={compareViewMode}
                    onChange={(event) => setCompareViewMode(event.target.value as "overlay" | "small_multiples")}
                  >
                    <option value="overlay">Overlay</option>
                    <option value="small_multiples">Small multiples</option>
                  </select>
                </label>
                <label className="flex items-center gap-1">
                  <input
                    type="checkbox"
                    checked={normalizeCompare}
                    onChange={(event) => setNormalizeCompare(event.target.checked)}
                  />
                  Normalize per stage
                </label>
                <Button onClick={() => void loadCompare()} disabled={compareBusy}>
                  {compareBusy ? "Loading..." : "Refresh compare"}
                </Button>
              </div>
            </div>
            {(compareResults?.projects.length ?? 0) < 2 ? (
              <p className="text-sm text-muted-foreground">{compareStatusLine}</p>
            ) : (
              <>
                {compareViewMode === "overlay" ? (
                  <TokenRadarChart data={radarData} axisKey="stage" series={radarSeries} />
                ) : (
                  <div className="grid gap-3 md:grid-cols-2">
                    {projectRadarRows.map((project) => (
                      <div key={project.project_id} className="rounded-md border bg-muted/20 p-2">
                        <p className="mb-2 text-sm font-medium">{project.project_name}</p>
                        <TokenRadarChart
                          data={project.rows}
                          axisKey="stage"
                          series={[{ key: "value", label: project.project_name, color: "#0ea5e9" }]}
                          height={260}
                        />
                      </div>
                    ))}
                  </div>
                )}
                <div className="overflow-x-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Project</TableHead>
                        <TableHead className="text-right">Retest Waste</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {compareResults?.projects.map((project) => (
                        <TableRow key={project.project_id}>
                          <TableCell>{project.project_name || project.project_id}</TableCell>
                          <TableCell className="text-right">{(project.retest_waste_score * 100).toFixed(1)}%</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
                <p className="text-sm text-muted-foreground">{compareStatusLine}</p>
              </>
            )}
          </CardContent>
        </Card>
        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={() => void load()} disabled={busy}>
            {busy ? "Loading..." : "Refresh"}
          </Button>
          <Button
            variant="outline"
            onClick={() => {
              setPage(1);
              setFilters({
                project_id: "",
                issue_keys: [],
                run_status: "",
                stage: "",
                attempt: "",
                model: "",
                from: "",
                to: "",
                only_retried: false,
                only_with_test_stage: false,
              });
            }}
            disabled={busy}
          >
            Clear
          </Button>
          <label className="ml-auto flex items-center gap-2 text-sm">
            Page size
            <select
              className="h-9 rounded-md border border-input bg-background px-2"
              value={String(pageSize)}
              onChange={(event) => {
                setPageSize(Number(event.target.value));
                setPage(1);
              }}
            >
              <option value="10">10</option>
              <option value="20">20</option>
              <option value="50">50</option>
            </select>
          </label>
        </div>
        {!results ? (
          <p className="text-sm text-muted-foreground">{statusLine}</p>
        ) : (
          <>
            <div className="grid gap-2 md:grid-cols-4">
              {results.stages.map((stage) => (
                <div
                  key={stage.stage}
                  className="rounded border bg-muted/20 p-2"
                  style={{ borderLeft: `3px solid ${stageColor(stage.stage)}` }}
                >
                  <p className="text-sm font-semibold uppercase">{stage.stage}</p>
                  <p className="text-xs text-muted-foreground">Avg Δ Input: {formatNumber(stage.avg_delta)}</p>
                  <p className="text-xs text-muted-foreground">Avg Δ Uncached: {formatNumber(stage.avg_uncached_delta)}</p>
                  <p className="text-xs text-muted-foreground">Retry Impact: {stage.retry_impact_index.toFixed(2)}</p>
                  <p className="text-xs text-muted-foreground">Runs: {stage.run_count}</p>
                </div>
              ))}
              {results.stages.length === 0 ? (
                <div className="rounded border bg-muted/20 p-2">
                  <p className="text-sm text-muted-foreground">No stage diagnostics available for current filters.</p>
                </div>
              ) : null}
            </div>
            <div className="rounded border bg-muted/20 p-2 text-sm">
              <span className="text-muted-foreground">Retest waste score:</span>{" "}
              <span className="font-semibold">{(results.retest_waste_score * 100).toFixed(1)}%</span>
            </div>
            <div className="rounded-md border bg-muted/20 p-3">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Stage x attempt heatmap (avg delta input)
              </p>
              {results.heatmap.length === 0 ? (
                <p className="text-sm text-muted-foreground">No heatmap data in selected filters.</p>
              ) : (
                <div className="overflow-x-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Stage</TableHead>
                        <TableHead className="text-right">Attempt</TableHead>
                        <TableHead className="text-right">Avg Δ Input</TableHead>
                        <TableHead className="text-right">Avg Δ Uncached</TableHead>
                        <TableHead className="text-right">Samples</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {results.heatmap.map((cell) => {
                        const intensity = Math.min(1, Math.max(0, cell.avg_delta / 3000));
                        return (
                          <TableRow
                            key={`${cell.stage}-${cell.attempt}`}
                            style={{ backgroundColor: `rgba(245, 158, 11, ${intensity * 0.35})` }}
                          >
                            <TableCell>{cell.stage.toUpperCase()}</TableCell>
                            <TableCell className="text-right">{cell.attempt}</TableCell>
                            <TableCell className="text-right">{formatMetricValue(cell.avg_delta)}</TableCell>
                            <TableCell className="text-right">{formatMetricValue(cell.avg_uncached_delta)}</TableCell>
                            <TableCell className="text-right">{cell.sample_count}</TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </div>
              )}
            </div>
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Per-ticket stage usage</CardTitle>
                <CardDescription>Token I/O split by stage for each issue key in current filters.</CardDescription>
              </CardHeader>
              <CardContent className="space-y-3">
                <TokenStackedBarChart
                  data={issueStageChartData.slice(0, 20)}
                  xAxisKey="issue_key"
                  bars={[
                    { key: "pm", label: "PM", color: "#0ea5e9", stackId: "stage" },
                    { key: "dev", label: "Dev", color: "#22c55e", stackId: "stage" },
                    { key: "test", label: "Test", color: "#f59e0b", stackId: "stage" },
                    { key: "review", label: "Review", color: "#ef4444", stackId: "stage" },
                  ]}
                />
                <div className="overflow-x-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Issue</TableHead>
                        <TableHead>Stage</TableHead>
                        <TableHead className="text-right">Input</TableHead>
                        <TableHead className="text-right">Uncached</TableHead>
                        <TableHead className="text-right">Output</TableHead>
                        <TableHead className="text-right">Total I/O</TableHead>
                        <TableHead className="text-right">Δ Total I/O</TableHead>
                        <TableHead className="text-right">Run Count</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {(results?.issue_stage_totals ?? []).map((row) => (
                        <TableRow key={`${row.issue_key}-${row.stage}`}>
                          <TableCell>{row.issue_key}</TableCell>
                          <TableCell>{row.stage.toUpperCase()}</TableCell>
                          <TableCell className="text-right">{formatMetricValue(row.input)}</TableCell>
                          <TableCell className="text-right">{formatMetricValue(row.uncached_input)}</TableCell>
                          <TableCell className="text-right">{formatMetricValue(row.output)}</TableCell>
                          <TableCell className="text-right">{formatMetricValue(row.total_io)}</TableCell>
                          <TableCell className="text-right">{formatMetricValue(row.delta_total_io)}</TableCell>
                          <TableCell className="text-right">{row.run_count}</TableCell>
                        </TableRow>
                      ))}
                      {(results?.issue_stage_totals ?? []).length === 0 ? (
                        <TableRow>
                          <TableCell colSpan={8} className="text-center text-muted-foreground">
                            No per-ticket stage usage available for selected filters.
                          </TableCell>
                        </TableRow>
                      ) : null}
                    </TableBody>
                  </Table>
                </div>
              </CardContent>
            </Card>
            <div className="rounded-md border bg-muted/20 p-3">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Heavy command signatures</p>
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Command Signature</TableHead>
                      <TableHead>Stage</TableHead>
                      <TableHead className="text-right">Spike Count</TableHead>
                      <TableHead className="text-right">Avg Δ Input</TableHead>
                      <TableHead className="text-right">Avg Δ Uncached</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {results.heavy_commands.map((command) => (
                      <TableRow key={`${command.command_signature}-${command.stage}`}>
                        <TableCell className="font-mono text-xs">{command.command_signature || "unknown"}</TableCell>
                        <TableCell>{command.stage}</TableCell>
                        <TableCell className="text-right">{command.spike_count}</TableCell>
                        <TableCell className="text-right">{formatMetricValue(command.avg_delta)}</TableCell>
                        <TableCell className="text-right">{formatMetricValue(command.avg_uncached_delta)}</TableCell>
                      </TableRow>
                    ))}
                    {results.heavy_commands.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={5} className="text-center text-muted-foreground">
                          No heavy commands identified in selected filters.
                        </TableCell>
                      </TableRow>
                    ) : null}
                  </TableBody>
                </Table>
              </div>
            </div>
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Runtime vs token delta</CardTitle>
                <CardDescription>Scatter point = one token-turn row.</CardDescription>
              </CardHeader>
              <CardContent>
                {scatterData.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No runtime/token scatter data found.</p>
                ) : (
                  <TokenScatterChart
                    data={scatterData}
                    xAxisKey="x_runtime_ms"
                    yAxisKey="y_token_delta"
                    pointLabel="Token Δ"
                  />
                )}
              </CardContent>
            </Card>
            <div className="flex items-center justify-between gap-2">
              <p className="text-sm text-muted-foreground">{statusLine}</p>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  onClick={() => setPage((current) => Math.max(1, current - 1))}
                  disabled={busy || page <= 1}
                >
                  Previous
                </Button>
                <span className="text-sm">Page {page}</span>
                <Button
                  variant="outline"
                  onClick={() => setPage((current) => current + 1)}
                  disabled={busy || (results?.heavy_commands ?? []).length < pageSize}
                >
                  Next
                </Button>
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
