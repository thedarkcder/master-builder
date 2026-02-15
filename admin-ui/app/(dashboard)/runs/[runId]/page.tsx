"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  cancelRun,
  getRun,
  listRunEvents,
  listRunLogs,
  rerunRun,
  streamRunEvents,
  type RunEventRecord,
  type RunLogEventRecord,
  type RunRecord
} from "@/lib/api";

function isAbortLikeError(error: unknown): boolean {
  const message = (error as Error)?.message?.toLowerCase() ?? "";
  return message.includes("aborted");
}

function statusFromLifecycleEvent(eventType: string): RunRecord["status"] | null {
  if (eventType === "TASK_COMPLETED") {
    return "succeeded";
  }
  if (eventType === "RUN_FAILED" || eventType === "TASK_FAILED") {
    return "failed";
  }
  return null;
}

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
  const [events, setEvents] = useState<RunEventRecord[]>([]);
  const [logs, setLogs] = useState<RunLogEventRecord[]>([]);
  const [busy, setBusy] = useState(false);
  const [rerunBusy, setRerunBusy] = useState(false);
  const [forceRerunBusy, setForceRerunBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [logAgentFilter, setLogAgentFilter] = useState("all");
  const [logStageFilter, setLogStageFilter] = useState("all");
  const [logStreamFilter, setLogStreamFilter] = useState("all");

  const loadRun = useCallback(async () => {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      const [payload, runEvents, runLogs] = await Promise.all([
        getRun(credentials, params.runId),
        listRunEvents(credentials, params.runId, { limit: 200 }),
        listRunLogs(credentials, params.runId, { limit: 500 })
      ]);
      setRun(payload);
      setEvents(runEvents);
      setLogs(runLogs);
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to load run: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }, [credentials, params.runId]);

  useEffect(() => {
    if (ready && credentials) {
      void loadRun();
    }
  }, [ready, credentials, loadRun]);

  useEffect(() => {
    if (!run || !credentials) {
      return;
    }
    if (run.status !== "queued" && run.status !== "running") {
      return;
    }
    const controller = new AbortController();
    void streamRunEvents(
      credentials,
      params.runId,
      (event) => {
        if ((event as { event_kind?: string }).event_kind === "run_log" || "message" in event) {
          const logEvent = event as RunLogEventRecord;
          setLogs((prev) => {
            if (
              prev.some(
                (entry) =>
                  entry.recorded_at === logEvent.recorded_at &&
                  entry.stage === logEvent.stage &&
                  entry.stream === logEvent.stream &&
                  entry.message === logEvent.message
              )
            ) {
              return prev;
            }
            const next = [...prev, logEvent];
            return next.slice(-500);
          });
        } else {
          const lifecycleEvent = event as RunEventRecord;
          setEvents((prev) => {
            if (
              prev.some(
                (entry) =>
                  entry.event_type === lifecycleEvent.event_type &&
                  entry.recorded_at === lifecycleEvent.recorded_at &&
                  entry.agent_id === lifecycleEvent.agent_id
              )
            ) {
              return prev;
            }
            const next = [...prev, lifecycleEvent];
            return next.slice(-200);
          });
          const nextStatus = statusFromLifecycleEvent(lifecycleEvent.event_type);
          if (nextStatus) {
            setRun((prev) => (prev ? { ...prev, status: nextStatus } : prev));
          }
        }
      },
      controller.signal
    ).catch((error) => {
      if (controller.signal.aborted || isAbortLikeError(error)) {
        return;
      }
      setStatusLine(`Run event stream closed: ${(error as Error).message}`);
    });
    return () => {
      controller.abort();
    };
  }, [run, credentials, params.runId, loadRun]);

  async function handleRerun() {
    if (!credentials || !run) {
      return;
    }
    setRerunBusy(true);
    try {
      const nextRun = await rerunRun(credentials, run.run_id);
      setStatusLine(`Queued rerun ${nextRun.run_id} for ${nextRun.issue_key}.`);
      window.location.href = `/runs/${encodeURIComponent(nextRun.run_id)}`;
    } catch (error) {
      setStatusLine(`Failed to rerun: ${(error as Error).message}`);
    } finally {
      setRerunBusy(false);
    }
  }

  async function handleForceRerun() {
    if (!credentials || !run) {
      return;
    }
    setForceRerunBusy(true);
    try {
      const cancelled = await cancelRun(credentials, run.run_id);
      const nextRun = await rerunRun(credentials, cancelled.run_id);
      setStatusLine(`Force-cancelled ${cancelled.run_id} and queued rerun ${nextRun.run_id}.`);
      window.location.href = `/runs/${encodeURIComponent(nextRun.run_id)}`;
    } catch (error) {
      setStatusLine(`Failed to force rerun: ${(error as Error).message}`);
    } finally {
      setForceRerunBusy(false);
    }
  }

  const isRerunnable = Boolean(run);
  const isActiveRun = run?.status === "queued" || run?.status === "running";
  const liveStageUpdates = Array.isArray(run?.plan?.["live_stage_updates"])
    ? (run?.plan?.["live_stage_updates"] as Array<Record<string, unknown>>)
    : [];
  function selectedAgentStage(filter: string): string {
    if (filter === "tester") {
      return "test";
    }
    return filter;
  }
  const filteredLogs = useMemo(
    () =>
      logs.filter((entry) => {
        const agentMatch =
          logAgentFilter === "all" || entry.stage === selectedAgentStage(logAgentFilter);
        const stageMatch = logStageFilter === "all" || entry.stage === logStageFilter;
        const streamMatch = logStreamFilter === "all" || entry.stream === logStreamFilter;
        return agentMatch && stageMatch && streamMatch;
      }),
    [logs, logAgentFilter, logStageFilter, logStreamFilter]
  );
  const codePath = useMemo(() => {
    for (let idx = logs.length - 1; idx >= 0; idx -= 1) {
      const value = logs[idx]?.working_dir?.trim();
      if (value) {
        return value;
      }
    }
    return null;
  }, [logs]);

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
            {isRerunnable ? (
              <Button variant="default" onClick={() => void handleRerun()} disabled={rerunBusy}>
                {rerunBusy ? "Requeueing..." : "Rerun"}
              </Button>
            ) : null}
            {isActiveRun ? (
              <Button variant="secondary" onClick={() => void handleForceRerun()} disabled={forceRerunBusy}>
                {forceRerunBusy ? "Force rerunning..." : "Force Rerun"}
              </Button>
            ) : null}
            <Button asChild variant="outline">
              <Link href={run ? `/tenants/${encodeURIComponent(run.tenant_id)}/runs` : "/tenants/select"}>
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
                <strong>Issue:</strong>{" "}
                {run.issue_url && run.issue_key ? (
                  <Link className="text-primary hover:underline" href={run.issue_url} target="_blank" rel="noopener noreferrer">
                    {run.issue_key}
                  </Link>
                ) : (
                  run.issue_key || "None"
                )}
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
                <strong>Repository:</strong>{" "}
                {run.repo_url ? (
                  <Link className="text-primary hover:underline" href={run.repo_url} target="_blank" rel="noopener noreferrer">
                    {run.repo_url}
                  </Link>
                ) : (
                  "None"
                )}
              </p>
              <p className="md:col-span-2">
                <strong>Code Path:</strong>{" "}
                {codePath ? (
                  <>
                    <a
                      className="text-primary hover:underline"
                      href={`file://${encodeURI(codePath)}`}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      Open local path
                    </a>
                    <span className="ml-2 break-all text-muted-foreground">{codePath}</span>
                  </>
                ) : (
                  "Not available yet"
                )}
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
            <div className="rounded-md border bg-muted/20 p-3">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Live stage updates</p>
              {liveStageUpdates.length === 0 ? (
                <p className="text-xs text-muted-foreground">No live stage updates captured yet.</p>
              ) : (
                <ul className="space-y-2 text-xs">
                  {liveStageUpdates.map((entry, idx) => (
                    <li key={`live-stage-${idx}`} className="rounded border p-2">
                      <p>
                        <strong>{String(entry.stage ?? "unknown_stage")}</strong>
                      </p>
                      <p className="text-muted-foreground">{String(entry.recorded_at ?? "")}</p>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="rounded-md border bg-muted/20 p-3">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Agent events (live)</p>
              {events.length === 0 ? (
                <p className="text-xs text-muted-foreground">No agent events captured for this run yet.</p>
              ) : (
                <ul className="max-h-[260px] space-y-2 overflow-y-auto pr-1 text-xs">
                  {events.map((event, idx) => (
                    <li key={`${event.agent_id}-${event.recorded_at}-${idx}`} className="rounded border p-2">
                      <p>
                        <strong>{event.event_type}</strong> by {event.agent_id}
                      </p>
                      <p className="text-muted-foreground">{new Date(event.recorded_at).toLocaleString()}</p>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="rounded-md border bg-muted/20 p-3">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Agent logs (live)</p>
                <div className="flex items-center gap-2 text-xs">
                  <label className="flex items-center gap-1">
                    Agent
                    <select
                      className="rounded border bg-background px-2 py-1"
                      value={logAgentFilter}
                      onChange={(event) => setLogAgentFilter(event.target.value)}
                    >
                      <option value="all">All</option>
                      <option value="pm">pm</option>
                      <option value="dev">dev</option>
                      <option value="tester">tester</option>
                      <option value="review">review</option>
                    </select>
                  </label>
                  <label className="flex items-center gap-1">
                    Stage
                    <select
                      className="rounded border bg-background px-2 py-1"
                      value={logStageFilter}
                      onChange={(event) => setLogStageFilter(event.target.value)}
                    >
                      <option value="all">All</option>
                      <option value="pm">pm</option>
                      <option value="dev">dev</option>
                      <option value="test">test</option>
                      <option value="review">review</option>
                    </select>
                  </label>
                  <label className="flex items-center gap-1">
                    Stream
                    <select
                      className="rounded border bg-background px-2 py-1"
                      value={logStreamFilter}
                      onChange={(event) => setLogStreamFilter(event.target.value)}
                    >
                      <option value="all">All</option>
                      <option value="stdout">stdout</option>
                      <option value="stderr">stderr</option>
                    </select>
                  </label>
                </div>
              </div>
              {logs.length === 0 ? (
                <p className="text-xs text-muted-foreground">No agent logs captured for this run yet.</p>
              ) : filteredLogs.length === 0 ? (
                <p className="text-xs text-muted-foreground">No log lines match current filters.</p>
              ) : (
                <ul className="max-h-[320px] space-y-2 overflow-y-auto pr-1 text-xs">
                  {filteredLogs.map((entry, idx) => (
                    <li key={`${entry.recorded_at}-${idx}`} className="rounded border p-2">
                      <p>
                        <strong>{entry.agent_id}</strong> · <strong>{entry.stage}</strong>
                        {entry.attempt !== null ? ` #${entry.attempt}` : ""} [{entry.stream}]
                      </p>
                      <p className="whitespace-pre-wrap">{entry.message}</p>
                      <p className="text-muted-foreground">{new Date(entry.recorded_at).toLocaleString()}</p>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </>
        ) : (
          <p className="text-muted-foreground">Loading run details...</p>
        )}
      </CardContent>
    </Card>
  );
}
