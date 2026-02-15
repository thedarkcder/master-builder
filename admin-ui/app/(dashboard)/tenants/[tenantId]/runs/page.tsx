"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { listRuns, type RunRecord } from "@/lib/api";

export default function TenantRunsPage() {
  const params = useParams<{ tenantId: string }>();
  const { credentials, ready } = useAuth();
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [issueFilter, setIssueFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [prFilter, setPrFilter] = useState<"any" | "none" | "has_value">("any");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [loading, setLoading] = useState(false);
  const [statusLine, setStatusLine] = useState("Load runs to inspect execution state.");

  const tenantId = decodeURIComponent(params.tenantId);

  const loadRuns = useCallback(async () => {
    if (!credentials) {
      return;
    }
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
        offset: (page - 1) * pageSize,
      });
      setRuns(payload);
      setStatusLine(`Loaded ${payload.length} run(s) on page ${page}.`);
    } catch (error) {
      setStatusLine(`Failed loading runs: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [credentials, tenantId, issueFilter, statusFilter, prFilter, fromDate, toDate, page, pageSize]);

  useEffect(() => {
    if (ready && credentials) {
      void loadRuns();
    }
  }, [ready, credentials, loadRuns]);

  function statusBadge(status: string) {
    if (status === "succeeded") {
      return <Badge>{status}</Badge>;
    }
    if (status === "failed" || status === "blocked") {
      return <Badge variant="secondary">{status}</Badge>;
    }
    return <Badge variant="outline">{status}</Badge>;
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Runs</CardTitle>
        <CardDescription>Tenant-scoped run history and execution status.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-2 md:grid-cols-3">
          <Input value={issueFilter} onChange={(event) => setIssueFilter(event.target.value)} placeholder="Issue key or summary" />
          <Input value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} placeholder="Status filter" />
          <select
            className="h-10 rounded-md border border-input bg-background px-3 text-sm"
            value={prFilter}
            onChange={(event) => setPrFilter(event.target.value as "any" | "none" | "has_value")}
          >
            <option value="any">PR: Any</option>
            <option value="none">PR: None</option>
            <option value="has_value">PR: Has value</option>
          </select>
          <Input type="date" value={fromDate} onChange={(event) => setFromDate(event.target.value)} placeholder="From date" />
          <Input type="date" value={toDate} onChange={(event) => setToDate(event.target.value)} placeholder="To date" />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={() => void loadRuns()} disabled={loading}>
            {loading ? "Loading..." : "Refresh"}
          </Button>
          <Button
            variant="outline"
            onClick={() => {
              setPage(1);
              void loadRuns();
            }}
            disabled={loading}
          >
            Apply filters
          </Button>
          <Button
            variant="outline"
            onClick={() => {
              setIssueFilter("");
              setStatusFilter("");
              setPrFilter("any");
              setFromDate("");
              setToDate("");
              setPage(1);
            }}
            disabled={loading}
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
              <option value="25">25</option>
              <option value="50">50</option>
              <option value="100">100</option>
            </select>
          </label>
        </div>

        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Run</TableHead>
              <TableHead>Issue</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>PR</TableHead>
              <TableHead>Created</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {runs.map((run) => (
              <TableRow key={run.run_id}>
                <TableCell className="font-medium">
                  <Link className="text-primary hover:underline" href={`/runs/${encodeURIComponent(run.run_id)}`}>
                    {run.issue_summary?.trim() || run.issue_key || run.run_id}
                  </Link>
                  <p className="text-xs text-muted-foreground">{run.run_id}</p>
                </TableCell>
                <TableCell>
                  {run.issue_url ? (
                    <Link className="text-primary hover:underline" href={run.issue_url} target="_blank" rel="noopener noreferrer">
                      {run.issue_key}
                    </Link>
                  ) : (
                    run.issue_key
                  )}
                </TableCell>
                <TableCell>{statusBadge(run.status)}</TableCell>
                <TableCell>
                  {run.pr_url ? (
                    <Link className="text-sm text-primary hover:underline" href={run.pr_url} target="_blank">
                      Open PR
                    </Link>
                  ) : (
                    <span className="text-xs text-muted-foreground">None</span>
                  )}
                </TableCell>
                <TableCell>{new Date(run.created_at).toLocaleString()}</TableCell>
              </TableRow>
            ))}
            {runs.length === 0 ? (
              <TableRow>
                <TableCell colSpan={5} className="text-center text-muted-foreground">
                  No runs found for current filters.
                </TableCell>
              </TableRow>
            ) : null}
          </TableBody>
        </Table>

        <div className="flex items-center justify-between gap-2">
          <p className="text-sm text-muted-foreground">{statusLine}</p>
          <div className="flex items-center gap-2">
            <Button variant="outline" onClick={() => setPage((current) => Math.max(1, current - 1))} disabled={loading || page <= 1}>
              Previous
            </Button>
            <span className="text-sm">Page {page}</span>
            <Button variant="outline" onClick={() => setPage((current) => current + 1)} disabled={loading || runs.length < pageSize}>
              Next
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
