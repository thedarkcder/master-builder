"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ExternalLink, SlidersHorizontal, X } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { listRuns, RUN_STATUSES, type RunRecord, type RunStatus } from "@/lib/api";
import { formatTimestamp } from "@/lib/datetime";
import { cn } from "@/lib/utils";

function statusBorderClass(status: string): string {
  const s = status?.toLowerCase() ?? "";
  if (s === "succeeded") return "border-l-success";
  if (s === "failed" || s === "blocked") return "border-l-destructive";
  if (s === "running") return "border-l-warning";
  if (s === "queued") return "border-l-info";
  return "border-l-border";
}

export default function TenantRunsPage() {
  const params = useParams<{ tenantId: string }>();
  const { credentials, ready } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);

  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [totalLoaded, setTotalLoaded] = useState<number | null>(null);
  const [issueFilter, setIssueFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<RunStatus | "all">("all");
  const [prFilter, setPrFilter] = useState<"any" | "none" | "has_value">("any");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<25 | 50 | 100>(25);

  const loadRuns = useCallback(async () => {
    if (!credentials) return;
    setLoading(true);
    try {
      const from = fromDate ? new Date(`${fromDate}T00:00:00.000Z`).toISOString() : undefined;
      const to = toDate ? new Date(`${toDate}T23:59:59.999Z`).toISOString() : undefined;
      const payload = await listRuns(credentials, {
        tenantId,
        issue: issueFilter || undefined,
        status: statusFilter === "all" ? undefined : statusFilter,
        prState: prFilter === "any" ? undefined : prFilter,
        from,
        to,
        limit: pageSize,
        offset: (page - 1) * pageSize
      });
      setRuns(payload);
      setTotalLoaded(payload.length);
    } catch {
      setTotalLoaded(null);
    } finally {
      setLoading(false);
    }
  }, [credentials, tenantId, issueFilter, statusFilter, prFilter, fromDate, toDate, page, pageSize]);

  useEffect(() => {
    if (ready && credentials) void loadRuns();
  }, [ready, credentials, loadRuns]);

  function clearFilters() {
    setIssueFilter("");
    setStatusFilter("all");
    setPrFilter("any");
    setFromDate("");
    setToDate("");
    setPage(1);
  }

  const hasFilters = issueFilter || statusFilter !== "all" || prFilter !== "any" || fromDate || toDate;

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
                className="h-8 w-36 text-sm"
                value={issueFilter}
                onChange={(e) => {
                  setIssueFilter(e.target.value);
                  setPage(1);
                }}
                placeholder="Issue key"
              />
              <select
                className="h-8 w-32 rounded-md border border-input bg-background px-2 text-sm"
                value={statusFilter}
                onChange={(e) => {
                  setStatusFilter(e.target.value as RunStatus | "all");
                  setPage(1);
                }}
              >
                <option value="all">Status: Any</option>
                {RUN_STATUSES.map((status) => (
                  <option key={status} value={status}>
                    {status}
                  </option>
                ))}
              </select>
              <select
                className="h-8 rounded-md border border-input bg-background px-2 text-sm"
                value={prFilter}
                onChange={(e) => {
                  setPrFilter(e.target.value as "any" | "none" | "has_value");
                  setPage(1);
                }}
              >
                <option value="any">PR: Any</option>
                <option value="none">PR: None</option>
                <option value="has_value">PR: Has PR</option>
              </select>
              <Input
                className="h-8 w-32 text-sm"
                type="date"
                value={fromDate}
                onChange={(e) => {
                  setFromDate(e.target.value);
                  setPage(1);
                }}
                placeholder="From"
              />
              <Input
                className="h-8 w-32 text-sm"
                type="date"
                value={toDate}
                onChange={(e) => {
                  setToDate(e.target.value);
                  setPage(1);
                }}
                placeholder="To"
              />

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
                  onChange={(e) => {
                    setPageSize(Number(e.target.value) as 25 | 50 | 100);
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

        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Run</TableHead>
              <TableHead>Issue</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Failure</TableHead>
              <TableHead>PR</TableHead>
              <TableHead>Created</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody className={cn(loading && "opacity-50 pointer-events-none transition-opacity")}>
            {runs.map((run) => (
              <TableRow
                key={run.run_id}
                className={cn("border-l-2", statusBorderClass(run.status))}
              >
                <TableCell className="font-medium">
                  <Link
                    className="text-primary hover:underline"
                    href={`/${encodeURIComponent(tenantId)}/runs/${encodeURIComponent(run.run_id)}`}
                  >
                    {run.issue_summary?.trim() || run.issue_key || run.run_id}
                  </Link>
                  <p className="text-xs text-muted-foreground">{run.run_id}</p>
                </TableCell>
                <TableCell>
                  {run.issue_url ? (
                    <Link
                      className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                      href={run.issue_url}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {run.issue_key}
                      <ExternalLink className="h-3 w-3" />
                    </Link>
                  ) : (
                    <span className="text-xs">{run.issue_key}</span>
                  )}
                </TableCell>
                <TableCell><StatusBadge status={run.status} /></TableCell>
                <TableCell className="max-w-[340px]">
                  {run.last_error ? (
                    <p className="line-clamp-2 text-xs text-destructive" title={run.last_error}>
                      {run.last_error}
                    </p>
                  ) : (
                    <span className="text-xs text-muted-foreground">—</span>
                  )}
                </TableCell>
                <TableCell>
                  {run.pr_url ? (
                    <Link
                      className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                      href={run.pr_url}
                      target="_blank"
                    >
                      PR <ExternalLink className="h-3 w-3" />
                    </Link>
                  ) : (
                    <span className="text-xs text-muted-foreground">—</span>
                  )}
                </TableCell>
                <TableCell className="text-sm text-muted-foreground">
                  {formatTimestamp(run.created_at)}
                </TableCell>
              </TableRow>
            ))}
            {runs.length === 0 && !loading ? (
              <TableRow>
                <TableCell colSpan={6} className="py-8 text-center text-sm text-muted-foreground">
                  No runs found for the current filters.
                </TableCell>
              </TableRow>
            ) : null}
          </TableBody>
        </Table>

        <div className="flex items-center justify-between border-t px-5 py-3">
          <span className="text-xs text-muted-foreground">
            {totalLoaded !== null ? `${totalLoaded} run${totalLoaded !== 1 ? "s" : ""}` : ""}
          </span>
          <div className="flex items-center gap-1">
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              onClick={() => setPage((prev) => Math.max(1, prev - 1))}
              disabled={loading || page <= 1}
            >
              Prev
            </Button>
            <span className="px-2 text-xs text-muted-foreground">Page {page}</span>
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-xs"
              onClick={() => setPage((prev) => prev + 1)}
              disabled={loading || runs.length < pageSize}
            >
              Next
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
