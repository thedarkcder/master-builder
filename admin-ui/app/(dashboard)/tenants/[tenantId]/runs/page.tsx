"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ExternalLink, RefreshCw, SlidersHorizontal, X } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { listRuns, type RunRecord } from "@/lib/api";
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
  const [issueFilter, setIssueFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [prFilter, setPrFilter] = useState<"any" | "none" | "has_value">("any");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [loading, setLoading] = useState(false);
  const [totalLoaded, setTotalLoaded] = useState<number | null>(null);

  const loadRuns = useCallback(async () => {
    if (!credentials) return;
    setLoading(true);
    try {
      const from = fromDate ? new Date(`${fromDate}T00:00:00.000Z`).toISOString() : undefined;
      const to = toDate ? new Date(`${toDate}T23:59:59.999Z`).toISOString() : undefined;
      const payload = await listRuns(credentials, {
        tenantId,
        issue: issueFilter || undefined,
        status: statusFilter || undefined,
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
    setStatusFilter("");
    setPrFilter("any");
    setFromDate("");
    setToDate("");
    setPage(1);
  }

  const hasFilters = issueFilter || statusFilter || prFilter !== "any" || fromDate || toDate;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Pipeline</h1>
        <p className="text-sm text-muted-foreground">All orchestration runs for this tenant.</p>
      </div>

      <Card>
        {/* Filter toolbar */}
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-end gap-2">
            <div className="flex items-center gap-1.5 text-muted-foreground">
              <SlidersHorizontal className="h-4 w-4" />
              <span className="text-xs font-medium uppercase tracking-wide">Filters</span>
            </div>

            <Input
              className="h-8 w-36 text-sm"
              value={issueFilter}
              onChange={(e) => { setIssueFilter(e.target.value); setPage(1); }}
              placeholder="Issue key"
            />
            <Input
              className="h-8 w-28 text-sm"
              value={statusFilter}
              onChange={(e) => { setStatusFilter(e.target.value); setPage(1); }}
              placeholder="Status"
            />
            <select
              className="h-8 rounded-md border border-input bg-background px-2 text-sm"
              value={prFilter}
              onChange={(e) => { setPrFilter(e.target.value as "any" | "none" | "has_value"); setPage(1); }}
            >
              <option value="any">PR: Any</option>
              <option value="none">PR: None</option>
              <option value="has_value">PR: Has PR</option>
            </select>
            <Input
              className="h-8 w-32 text-sm"
              type="date"
              value={fromDate}
              onChange={(e) => { setFromDate(e.target.value); setPage(1); }}
              placeholder="From"
            />
            <Input
              className="h-8 w-32 text-sm"
              type="date"
              value={toDate}
              onChange={(e) => { setToDate(e.target.value); setPage(1); }}
              placeholder="To"
            />

            <div className="ml-auto flex items-center gap-1.5">
              {hasFilters ? (
                <Button variant="ghost" size="sm" className="h-8 text-xs text-muted-foreground" onClick={clearFilters}>
                  <X className="mr-1 h-3 w-3" />
                  Clear
                </Button>
              ) : null}
              <Button variant="outline" size="sm" className="h-8" onClick={() => void loadRuns()} disabled={loading}>
                <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", loading && "animate-spin")} />
                Refresh
              </Button>
              <select
                className="h-8 rounded-md border border-input bg-background px-2 text-xs"
                value={String(pageSize)}
                onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1); }}
              >
                <option value="25">25 / page</option>
                <option value="50">50 / page</option>
                <option value="100">100 / page</option>
              </select>
            </div>
          </div>
        </CardHeader>

        <CardContent className="p-0">
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
            <TableBody>
              {runs.map((run) => (
                <TableRow
                  key={run.run_id}
                  className={cn("border-l-2", statusBorderClass(run.status))}
                >
                  <TableCell className="font-medium">
                    <Link
                      className="text-primary hover:underline"
                      href={`/tenants/${encodeURIComponent(tenantId)}/runs/${encodeURIComponent(run.run_id)}`}
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
                    {new Date(run.created_at).toLocaleString()}
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

          {/* Pagination footer */}
          <div className="flex items-center justify-between border-t px-6 py-3">
            <span className="text-xs text-muted-foreground">
              {totalLoaded !== null ? `${totalLoaded} run${totalLoaded !== 1 ? "s" : ""} on page ${page}` : ""}
            </span>
            <div className="flex items-center gap-1">
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={loading || page <= 1}
              >
                ← Prev
              </Button>
              <span className="px-2 text-xs text-muted-foreground">Page {page}</span>
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                onClick={() => setPage((p) => p + 1)}
                disabled={loading || runs.length < pageSize}
              >
                Next →
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
