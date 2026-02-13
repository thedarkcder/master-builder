"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { getRun, type RunRecord } from "@/lib/api";

function statusBadge(status: string) {
  if (status === "succeeded") {
    return <Badge>{status}</Badge>;
  }
  if (status === "failed" || status === "blocked") {
    return <Badge variant="secondary">{status}</Badge>;
  }
  return <Badge variant="outline">{status}</Badge>;
}

export default function RunDetailPage() {
  const params = useParams<{ runId: string }>();
  const { credentials, ready } = useAuth();
  const [run, setRun] = useState<RunRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("");

  async function loadRun() {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      const payload = await getRun(credentials, params.runId);
      setRun(payload);
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to load run: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) {
      void loadRun();
    }
  }, [ready, credentials, params.runId]);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle>{run?.run_id ?? params.runId}</CardTitle>
            <CardDescription>Run details</CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" onClick={() => void loadRun()} disabled={busy}>
              {busy ? "Refreshing..." : "Refresh"}
            </Button>
            <Button asChild variant="outline">
              <Link href="/runs">
                <ArrowLeft className="mr-2 h-4 w-4" />
                Back to Runs
              </Link>
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {statusLine ? <p className="rounded-md border px-3 py-2 text-red-700">{statusLine}</p> : null}
        {run ? (
          <>
            <div className="grid gap-3 md:grid-cols-2">
              <p>
                <strong>Tenant:</strong> {run.tenant_id}
              </p>
              <p>
                <strong>Project:</strong> {run.project_id ?? "None"}
              </p>
              <p>
                <strong>Issue:</strong> {run.issue_key || "None"}
              </p>
              <p>
                <strong>Status:</strong> {statusBadge(run.status)}
              </p>
              <p>
                <strong>Created:</strong> {new Date(run.created_at).toLocaleString()}
              </p>
              <p>
                <strong>Started:</strong> {run.started_at ? new Date(run.started_at).toLocaleString() : "Not started"}
              </p>
              <p>
                <strong>Finished:</strong> {run.finished_at ? new Date(run.finished_at).toLocaleString() : "Not finished"}
              </p>
              <p>
                <strong>Branch:</strong> {run.branch ?? "None"}
              </p>
              <p className="md:col-span-2">
                <strong>Repository:</strong> {run.repo_url ?? "None"}
              </p>
              <p className="md:col-span-2">
                <strong>PR:</strong>{" "}
                {run.pr_url ? (
                  <Link className="text-primary hover:underline" href={run.pr_url} target="_blank">
                    {run.pr_url}
                  </Link>
                ) : (
                  "None"
                )}
              </p>
              <p className="md:col-span-2">
                <strong>Last error:</strong> {run.last_error ?? "None"}
              </p>
            </div>
            <div className="rounded-md border bg-muted/40 p-3">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Plan JSON</p>
              <pre className="max-h-[360px] overflow-auto whitespace-pre-wrap text-xs">
                {run.plan ? JSON.stringify(run.plan, null, 2) : "No plan captured for this run."}
              </pre>
            </div>
          </>
        ) : (
          <p className="text-muted-foreground">Loading run details...</p>
        )}
      </CardContent>
    </Card>
  );
}

