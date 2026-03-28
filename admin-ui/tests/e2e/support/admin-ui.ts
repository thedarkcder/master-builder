import type { Page, Route } from "@playwright/test";

import {
  AUTH_COOKIE_KEY,
  AUTH_COOKIE_TTL_SECONDS,
  AUTH_STORAGE_KEY,
  DEFAULT_API_BASE_URL,
} from "../../../lib/auth-constants";
import type {
  ProjectAutomationExecutionRecord,
  ProjectAutomationRecord,
  ProjectRecord,
  RunEventRecord,
  RunLogEventRecord,
  RunRecord,
  RunRerunPayload,
  TenantRecord,
  TokenTimelineRecord,
} from "../../../lib/api";

export const ADMIN_ACCESS_TOKEN = "playwright-admin-token";

type AdminRouteHandler = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  pathname: string | RegExp;
  handler: (route: Route, url: URL) => Promise<void> | void;
};

export async function seedAdminSession(page: Page, accessToken = ADMIN_ACCESS_TOKEN): Promise<void> {
  await page.context().addCookies([
    {
      name: AUTH_COOKIE_KEY,
      value: "1",
      url: "http://localhost:4100",
      sameSite: "Lax",
    },
  ]);
  await page.addInitScript(
    ({ storageKey, apiBaseUrl, token, authCookieKey, authCookieTtlSeconds }) => {
      window.localStorage.setItem(
        storageKey,
        JSON.stringify({
          apiBaseUrl,
          accessToken: token,
        }),
      );
      document.cookie = `${authCookieKey}=1; path=/; max-age=${authCookieTtlSeconds}; samesite=lax`;
    },
    {
      storageKey: AUTH_STORAGE_KEY,
      apiBaseUrl: DEFAULT_API_BASE_URL,
      token: accessToken,
      authCookieKey: AUTH_COOKIE_KEY,
      authCookieTtlSeconds: AUTH_COOKIE_TTL_SECONDS,
    },
  );
}

export async function installAdminApiMocks(page: Page, handlers: AdminRouteHandler[]): Promise<void> {
  await page.route(`${DEFAULT_API_BASE_URL}/api/admin/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method().toUpperCase();
    for (const candidate of handlers) {
      if (candidate.method && candidate.method !== method) {
        continue;
      }
      if (typeof candidate.pathname === "string") {
        if (candidate.pathname !== url.pathname) {
          continue;
        }
      } else if (!candidate.pathname.test(url.pathname)) {
        continue;
      }
      await candidate.handler(route, url);
      return;
    }
    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({
        detail: `Unhandled admin API request: ${method} ${url.pathname}`,
      }),
    });
  });
}

export async function fulfillJson(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

export function makeTenant(overrides: Partial<TenantRecord> = {}): TenantRecord {
  return {
    tenant_id: "example",
    name: "Route 25",
    is_enabled: true,
    jira: {
      connection_id: null,
      project_keys: ["ROUTE"],
      ready_statuses: ["Ready"],
      ready_jql: null,
      ready_label: "ready",
      in_progress_label: "in-progress",
      blocked_label: "blocked",
      done_label: "done",
      webhook_secret_ref: null,
    },
    github: {
      webhook_secret_ref: null,
      installation_id: null,
    },
    repos: {
      github_repository: "thedarkcder/girl-power",
    },
    policy: {
      allow_jira_transitions: true,
      allow_pr_creation: true,
      allow_code_reviews: true,
      allow_pr_remediation: true,
      allow_manual_pr_fix_requests: true,
      allow_label_mutations: true,
      allow_auto_merge: false,
      max_runtime_minutes: 120,
      max_dev_test_review_loops: 3,
      max_pr_auto_remediation_loops: 2,
      max_concurrent_runs: 2,
      allowed_commands: [],
      require_agents_md: false,
      knowledge_base_enabled: false,
      knowledge_auto_answer_mode: "safe",
      codex_model: "gpt-5.4",
      codex_reasoning_effort: "medium",
    },
    discord: null,
    created_at: "2026-03-27T16:00:00Z",
    updated_at: "2026-03-27T16:00:00Z",
    ...overrides,
  };
}

export function makeProject(overrides: Partial<ProjectRecord> = {}): ProjectRecord {
  return {
    project_id: "example-default",
    tenant_id: "example",
    name: "Route 25 Default",
    github_repository: "thedarkcder/girl-power",
    jira_project_key: "GP",
    policy_overrides: {},
    environment: {},
    secret_refs: {},
    discord: null,
    effective_policy: makeTenant().policy,
    is_archived: false,
    created_at: "2026-03-27T16:00:00Z",
    updated_at: "2026-03-27T16:00:00Z",
    ...overrides,
  };
}

export function makeProjectAutomationExecution(
  overrides: Partial<ProjectAutomationExecutionRecord> = {},
): ProjectAutomationExecutionRecord {
  return {
    execution_id: "exec-1",
    automation_id: "automation-1",
    scheduled_for: "2026-03-28T09:00:00Z",
    window_start_at: "2026-03-28T08:00:00Z",
    window_end_at: "2026-03-28T09:00:00Z",
    status: "succeeded",
    dedupe_key: "dedupe-1",
    started_at: "2026-03-28T08:01:00Z",
    completed_at: "2026-03-28T08:05:00Z",
    discord_message_id: "9876543210",
    last_error: null,
    created_at: "2026-03-28T08:00:00Z",
    updated_at: "2026-03-28T08:05:00Z",
    ...overrides,
  };
}

export function makeProjectAutomation(overrides: Partial<ProjectAutomationRecord> = {}): ProjectAutomationRecord {
  return {
    automation_id: "automation-1",
    project_id: "example-default",
    tenant_id: "example",
    kind: "standup_voice_brief",
    enabled: true,
    timezone: "UTC",
    days_of_week: [1, 2, 3, 4, 5],
    local_time: "09:30",
    delivery_text_channel_id: "123456789012345678",
    voice_id: "alloy",
    fallback_lookback_hours: 24,
    last_successful_window_end_at: "2026-03-28T09:00:00Z",
    next_run_at: "2026-03-29T09:30:00Z",
    executions: [makeProjectAutomationExecution()],
    created_at: "2026-03-27T16:00:00Z",
    updated_at: "2026-03-28T08:05:00Z",
    ...overrides,
  };
}

export function makeRun(overrides: Partial<RunRecord> = {}): RunRecord {
  return {
    run_id: "5de2cedf-b7ae-400c-a53c-3beecf078a51",
    tenant_id: "example",
    project_id: "example-default",
    issue_key: "GP-124",
    issue_summary: "Fix rerun lifecycle regressions",
    issue_url: "https://jira.example.test/browse/GP-124",
    repo_url: "https://github.com/thedarkcder/girl-power",
    branch: "feature/GP-124",
    pr_url: "https://github.com/thedarkcder/girl-power/pull/21",
    dev_session_id: "019d1c32-5b72-7c53-bad0-8be1f842b1c2",
    pm_session_id: "019d1c2d-c6ea-7370-b5f8-1e8ddbe79e8d",
    orchestrated_session_id: null,
    status: "failed",
    last_error: "Recovered stale running run after heartbeat timeout.",
    created_at: "2026-03-27T16:50:00Z",
    started_at: "2026-03-27T16:50:10Z",
    finished_at: "2026-03-27T17:08:27Z",
    plan: {
      stage_checkpoints: {
        pm: {
          status: "completed",
          completed_at: "2026-03-27T16:55:00Z",
          summary: "PM plan captured.",
        },
        dev: {
          status: "completed",
          completed_at: "2026-03-27T17:02:57Z",
          summary: "PR created and code pushed.",
        },
      },
      execution_context: {
        integration_branch: "feature/GP-124",
        execution_branch: "run/gp-124/5de2cedf-b7ae-400c-a53c-3beecf078a51",
        base_branch: "main",
        execution_repo_dir: "/tmp/worktree",
      },
    },
    ...overrides,
  };
}

export function makeTokenTimeline(run: RunRecord): TokenTimelineRecord {
  return {
    run_id: run.run_id,
    issue_key: run.issue_key,
    model: "gpt-5.4",
    status: run.status,
    totals: {
      input: 1_000,
      uncached_input: 500,
      output: 250,
      cached_input: 500,
      cache_ratio: 0.5,
      total_io: 1_250,
      avg_runtime_ms: 5_000,
      p95_runtime_ms: 6_000,
    },
    turns: [],
  };
}

export function makeStageInvocationLogs(options: {
  runId?: string;
  stage: "pm" | "dev" | "test" | "review";
  invocationId: string;
  command: string;
  startedAt: string;
  finishedAt?: string;
  codexSessionId?: string;
}): RunLogEventRecord[] {
  const runId = options.runId ?? "5de2cedf-b7ae-400c-a53c-3beecf078a51";
  const startedMessage = JSON.stringify({
    event_kind: "stage_invocation_started",
    codex_session_id: options.codexSessionId,
  });
  const rows: RunLogEventRecord[] = [
    {
      run_id: runId,
      issue_key: "GP-124",
      project_id: "example-default",
      agent_id: options.stage,
      invocation_id: options.invocationId,
      channel: null,
      command: options.command,
      working_dir: "/tmp/worktree",
      stage: "telemetry",
      attempt: 1,
      stream: "system",
      message: startedMessage,
      recorded_at: options.startedAt,
    },
  ];
  if (options.finishedAt) {
    rows.push({
      run_id: runId,
      issue_key: "GP-124",
      project_id: "example-default",
      agent_id: options.stage,
      invocation_id: options.invocationId,
      channel: null,
      command: options.command,
      working_dir: "/tmp/worktree",
      stage: "telemetry",
      attempt: 1,
      stream: "system",
      message: JSON.stringify({
        event_kind: "stage_invocation_finished",
        status: "completed",
        duration_ms: 60_000,
        codex_session_id: options.codexSessionId,
      }),
      recorded_at: options.finishedAt,
    });
  }
  return rows;
}

export async function mockRunDetailApis(
  page: Page,
  options: {
    run: RunRecord;
    events?: RunEventRecord[];
    logs?: RunLogEventRecord[];
    tokenTimeline?: TokenTimelineRecord;
    tenant?: TenantRecord;
    projects?: ProjectRecord[];
    rerunResponse?: RunRecord;
    onRerun?: (payload: RunRerunPayload) => void;
  },
): Promise<void> {
  const tenant = options.tenant ?? makeTenant({ tenant_id: options.run.tenant_id });
  const projects = options.projects ?? [makeProject({ tenant_id: options.run.tenant_id, project_id: options.run.project_id ?? "example-default" })];
  const nextRun =
    options.rerunResponse ??
    makeRun({
      run_id: "8e8957f2-79f8-4dc8-8deb-786b2c93828d",
      status: "queued",
      created_at: "2026-03-27T17:10:00Z",
      started_at: null,
      finished_at: null,
      plan: { pre_check: { outcome: "ready_for_agent" } },
      pr_url: null,
    });
  await installAdminApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/admin/auth/me",
      handler: (route) => fulfillJson(route, { username: "admin" }),
    },
    {
      method: "GET",
      pathname: /^\/api\/admin\/runs\/[^/]+$/,
      handler: (route, url) => {
        const runId = decodeURIComponent(url.pathname.split("/").at(-1) ?? "");
        if (runId === options.run.run_id) {
          return fulfillJson(route, options.run);
        }
        if (runId === nextRun.run_id) {
          return fulfillJson(route, nextRun);
        }
        return route.fulfill({
          status: 404,
          contentType: "application/json",
          body: JSON.stringify({ detail: `Unknown run ${runId}` }),
        });
      },
    },
    {
      method: "GET",
      pathname: /^\/api\/admin\/runs\/[^/]+\/events$/,
      handler: (route) => fulfillJson(route, options.events ?? []),
    },
    {
      method: "GET",
      pathname: /^\/api\/admin\/runs\/[^/]+\/logs$/,
      handler: (route) => fulfillJson(route, options.logs ?? []),
    },
    {
      method: "GET",
      pathname: new RegExp(`^/api/admin/tenants/${encodeURIComponent(options.run.tenant_id)}/runs/[^/]+/token-timeline$`),
      handler: (route, url) => {
        const runId = decodeURIComponent(url.pathname.split("/").at(-2) ?? "");
        if (runId === nextRun.run_id) {
          return fulfillJson(route, makeTokenTimeline(nextRun));
        }
        return fulfillJson(route, options.tokenTimeline ?? makeTokenTimeline(options.run));
      },
    },
    {
      method: "GET",
      pathname: `/api/admin/tenants/${encodeURIComponent(options.run.tenant_id)}`,
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: `/api/admin/tenants/${encodeURIComponent(options.run.tenant_id)}/projects`,
      handler: (route) => fulfillJson(route, projects),
    },
    {
      method: "POST",
      pathname: `/api/admin/runs/${encodeURIComponent(options.run.run_id)}/rerun`,
      handler: async (route) => {
        const payload = JSON.parse(route.request().postData() ?? "{}") as RunRerunPayload;
        options.onRerun?.(payload);
        await fulfillJson(route, nextRun);
      },
    },
  ]);
}
