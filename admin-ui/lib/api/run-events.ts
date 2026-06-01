import { readNdjsonStream, request, type Credentials } from "@/lib/api/http";
import type { ProjectDeploymentReleaseRecord } from "@/lib/api/deployments";

export const RUN_STATUSES = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "blocked",
  "cancelled",
] as const;

export type RunStatus = (typeof RUN_STATUSES)[number];

export type RunRecord = {
  run_id: string;
  workflow_id: string;
  workflow_execution_id: string;
  attempt_number: number;
  parent_run_id: string | null;
  entry_mode: string;
  entry_stage: string | null;
  entry_checkpoint_id: string | null;
  tenant_id: string;
  project_id: string | null;
  issue_key: string;
  issue_summary: string | null;
  issue_url: string | null;
  repo_url: string | null;
  branch: string | null;
  pr_url: string | null;
  status: RunStatus;
  waiting_for_input: boolean;
  pending_input_request_id: string | null;
  last_error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  plan: Record<string, unknown> | null;
};

export type RunEventRecord = {
  event_type: string;
  run_id: string;
  issue_key: string | null;
  project_id: string | null;
  agent_id: string;
  recorded_at: string;
};

export type RuntimeLogEventRecord = {
  event_id: string;
  event_sequence?: number | string | null;
  run_id: string;
  issue_key: string | null;
  project_id: string | null;
  agent_id: string;
  invocation_id?: string | null;
  channel?: string | null;
  command?: string | null;
  working_dir: string | null;
  stage: string;
  attempt: number | null;
  stream: string;
  message: string;
  recorded_at: string;
};

export function listRuns(
  credentials: Credentials,
  params: {
    tenantId?: string;
    projectId?: string;
    status?: RunStatus;
    issue?: string;
    prState?: "none" | "has_value";
    from?: string;
    to?: string;
    limit?: number;
    offset?: number;
  },
): Promise<RunRecord[]> {
  const query = new URLSearchParams();
  if (params.tenantId) {
    query.set("tenant_id", params.tenantId);
  }
  if (params.projectId) {
    query.set("project_id", params.projectId);
  }
  if (params.status) {
    query.set("status", params.status);
  }
  if (params.issue) {
    query.set("issue", params.issue);
  }
  if (params.prState) {
    query.set("pr_state", params.prState);
  }
  if (params.from) {
    query.set("from", params.from);
  }
  if (params.to) {
    query.set("to", params.to);
  }
  if (typeof params.limit === "number") {
    query.set("limit", String(params.limit));
  }
  if (typeof params.offset === "number") {
    query.set("offset", String(params.offset));
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<RunRecord[]>(credentials, `/api/admin/runs${suffix}`);
}

export function getRun(credentials: Credentials, runId: string): Promise<RunRecord> {
  return request<RunRecord>(credentials, `/api/admin/runs/${encodeURIComponent(runId)}`);
}

export function cancelRun(credentials: Credentials, runId: string): Promise<RunRecord> {
  return request<RunRecord>(credentials, `/api/admin/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
  });
}

export function createRunPreview(
  credentials: Credentials,
  runId: string,
  options: { force?: boolean } = {},
): Promise<ProjectDeploymentReleaseRecord> {
  const query = new URLSearchParams();
  if (options.force) {
    query.set("force", "true");
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<ProjectDeploymentReleaseRecord>(credentials, `/api/admin/runs/${encodeURIComponent(runId)}/preview${suffix}`, {
    method: "POST",
  });
}

export function listRunEvents(
  credentials: Credentials,
  runId: string,
  params: { limit?: number } = {},
): Promise<RunEventRecord[]> {
  const query = new URLSearchParams();
  if (params.limit) {
    query.set("limit", String(params.limit));
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<RunEventRecord[]>(
    credentials,
    `/api/admin/runs/${encodeURIComponent(runId)}/events${suffix}`,
  );
}

export function listRunLogs(
  credentials: Credentials,
  runId: string,
  params: { limit?: number; beforeRecordedAt?: string; beforeEventId?: string } = {},
): Promise<RuntimeLogEventRecord[]> {
  const query = new URLSearchParams();
  if (params.limit) {
    query.set("limit", String(params.limit));
  }
  if (params.beforeRecordedAt) {
    query.set("before_recorded_at", params.beforeRecordedAt);
  }
  if (params.beforeEventId) {
    query.set("before_event_id", params.beforeEventId);
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<RuntimeLogEventRecord[]>(
    credentials,
    `/api/admin/runs/${encodeURIComponent(runId)}/logs${suffix}`,
  );
}

export async function streamRunEvents(
  credentials: Credentials,
  runId: string,
  onEvent: (event: RunEventRecord | (RuntimeLogEventRecord & { event_kind?: string })) => void,
  signal?: AbortSignal,
): Promise<void> {
  void credentials;
  const response = await fetch(`/api/bff/api/admin/runs/${encodeURIComponent(runId)}/events/stream`, {
    method: "GET",
    headers: {
      Accept: "application/x-ndjson",
    },
    signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(`${response.status}: unable to open run event stream`);
  }
  await readNdjsonStream<RunEventRecord | (RuntimeLogEventRecord & { event_kind?: string })>(response, onEvent, {
    signal,
    malformedMessage: "Malformed run event stream event",
  });
}
