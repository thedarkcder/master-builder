"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from "@/components/ui/table";
import { listRuns, type RunRecord } from "@/lib/api";

function statusBadge(status: string) {
  if (status === "succeeded") {
    return <Badge>{status}</Badge>;
  }
  if (status === "failed" || status === "blocked") {
    return <Badge variant="secondary">{status}</Badge>;
  }
  return <Badge variant="outline">{status}</Badge>;
}

export default function RunsPage() {
  const { credentials, ready } = useAuth();
  const [runs, setRuns] = useState<RunRecord[]>([]);

  const [tenantFilter, setTenantFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const [statusLine, setStatusLine] = useState("Load runs to inspect execution state.");

  async function loadRuns() {
    if (!credentials) {
      return;
    }

    setLoading(true);
    try {
      const payload = await listRuns(credentials, {
        tenantId: tenantFilter || undefined,
        status: statusFilter || undefined
      });
      setRuns(payload);
      setStatusLine(`Loaded ${payload.length} run(s).`);
    } catch (error) {
      setStatusLine(`Failed loading runs: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) {
      void loadRuns();
    }
  }, [ready, credentials]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Runs</CardTitle>
        <CardDescription>Filter and inspect orchestration runs.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-2 md:grid-cols-[1fr_1fr_auto]">
          <Input
            value={tenantFilter}
            onChange={(event) => setTenantFilter(event.target.value)}
            placeholder="Tenant filter"
          />
          <Input
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value)}
            placeholder="Status filter"
          />
          <Button onClick={() => void loadRuns()} disabled={loading}>
            {loading ? "Loading..." : "Refresh"}
          </Button>
        </div>

        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Run</TableHead>
              <TableHead>Tenant</TableHead>
              <TableHead>Issue</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>PR</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {runs.map((run) => (
              <TableRow key={run.run_id}>
                <TableCell className="font-medium">
                  <Link className="text-primary hover:underline" href={`/runs/${encodeURIComponent(run.run_id)}`}>
                    {run.run_id}
                  </Link>
                </TableCell>
                <TableCell>{run.tenant_id}</TableCell>
                <TableCell>{run.issue_key}</TableCell>
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
                <TableCell>
                  <Button asChild size="sm" variant="outline">
                    <Link href={`/runs/${encodeURIComponent(run.run_id)}`}>View</Link>
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        <p className="text-sm text-muted-foreground">{statusLine}</p>
      </CardContent>
    </Card>
  );
}
