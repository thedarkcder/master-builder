import { encode } from "next-auth/jwt";
import type { Page, Route } from "@playwright/test";

import {
  AUTH_COOKIE_TTL_SECONDS,
  DEFAULT_API_BASE_URL,
} from "../../../lib/auth-constants";
import type {
  ProjectRecord,
  RunEventRecord,
  RunLogEventRecord,
  RunRecord,
  RunRerunPayload,
  AuthenticatedPrincipalRecord,
  DeliverySummaryRecord,
  MembershipRecord,
  TenantDiscordIdentityRecord,
  TenantInviteRecord,
  TenantMemberRecord,
  TenantRecord,
  TenantTeamRecord,
  TokenTimelineRecord,
} from "../../../lib/api";

export const ADMIN_ACCESS_TOKEN = "playwright-admin-token";
export const TENANT_ACCESS_TOKEN = "playwright-tenant-token";

const APP_BASE_URL = "http://localhost:4100";
const BACKEND_BASE_URL = DEFAULT_API_BASE_URL;
const AUTH_SECRET = process.env.AUTH_SECRET ?? process.env.NEXTAUTH_SECRET ?? "local-dev-authjs-secret";
const AUTH_SESSION_COOKIE_NAME = "authjs.session-token";
const AUTH_SESSION_COOKIE_SALT = "authjs.session-token";

type AdminRouteHandler = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  pathname: string | RegExp;
  handler: (route: Route, url: URL) => Promise<void> | void;
};

type AppRouteHandler = AdminRouteHandler;

type TenantSessionSeed = {
  principal: AuthenticatedPrincipalRecord;
  accessToken?: string;
  userName?: string | null;
  userEmail?: string | null;
};

export async function seedAdminSession(page: Page, accessToken = ADMIN_ACCESS_TOKEN): Promise<void> {
  await seedAuthenticatedSession(page, {
    principal: makePlatformAdminPrincipal(),
    accessToken,
    userName: "admin",
    userEmail: null,
  });
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

export async function installBffApiMocks(page: Page, handlers: AppRouteHandler[]): Promise<void> {
  await installApiMocks(page, APP_BASE_URL, handlers);
}

export async function installAppApiMocks(page: Page, handlers: AppRouteHandler[]): Promise<void> {
  await installApiMocks(page, APP_BASE_URL, handlers);
}

export async function installBackendApiMocks(page: Page, handlers: AppRouteHandler[]): Promise<void> {
  await installApiMocks(page, BACKEND_BASE_URL, handlers);
}

export async function seedTenantSession(
  page: Page,
  { principal, accessToken = TENANT_ACCESS_TOKEN, userEmail, userName }: TenantSessionSeed,
): Promise<void> {
  await seedAuthenticatedSession(page, { principal, accessToken, userEmail, userName });
}

async function seedAuthenticatedSession(
  page: Page,
  { principal, accessToken, userEmail, userName }: TenantSessionSeed,
): Promise<void> {
  const token = await encode({
    secret: AUTH_SECRET,
    salt: AUTH_SESSION_COOKIE_SALT,
    token: {
      sub: principal.user_id ?? principal.email ?? principal.username ?? "playwright-user",
      name: userName ?? principal.full_name ?? principal.username ?? principal.email ?? "Playwright User",
      email: userEmail ?? principal.email ?? null,
      accessToken,
      principal,
    },
    maxAge: AUTH_COOKIE_TTL_SECONDS,
  });

  await page.context().addCookies([
    {
      name: AUTH_SESSION_COOKIE_NAME,
      value: token,
      url: APP_BASE_URL,
      httpOnly: true,
      sameSite: "Lax",
    },
  ]);

  await installAuthSessionMock(page, { principal, userEmail, userName });
}

export async function mockCredentialSignIn(
  page: Page,
  seed: TenantSessionSeed,
): Promise<void> {
  await page.route(`${APP_BASE_URL}/api/auth/callback/credentials**`, async (route) => {
    await seedTenantSession(page, seed);
    const principal = seed.principal;
    const redirectUrl =
      principal.principal_type === "platform_super_admin"
        ? `${APP_BASE_URL}/tenants/select`
        : principal.memberships[0]
          ? `${APP_BASE_URL}/tenants/${encodeURIComponent(principal.memberships[0].tenant_id)}/dashboard`
          : `${APP_BASE_URL}/tenants/select`;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        status: 200,
        url: redirectUrl,
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
    tenant_id: "route25",
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
    experience: {
      default_mode: "technical",
    },
    setup_state: {},
    created_at: "2026-03-27T16:00:00Z",
    updated_at: "2026-03-27T16:00:00Z",
    ...overrides,
  };
}

export function makeProject(overrides: Partial<ProjectRecord> = {}): ProjectRecord {
  return {
    project_id: "route25-default",
    tenant_id: "route25",
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

export function makeRun(overrides: Partial<RunRecord> = {}): RunRecord {
  return {
    run_id: "5de2cedf-b7ae-400c-a53c-3beecf078a51",
    tenant_id: "route25",
    project_id: "route25-default",
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

export function makeMembership(overrides: Partial<MembershipRecord> = {}): MembershipRecord {
  return {
    membership_id: "membership-route25",
    tenant_id: "route25",
    role: "technical_member",
    permission_keys: ["analytics.business.view", "analytics.technical.view"],
    effective_mode: "technical",
    mode_override: null,
    onboarding_kind: "member_join",
    first_signed_in_at: "2026-03-27T16:00:00Z",
    onboarding_completed_at: "2026-03-27T16:30:00Z",
    onboarding_version: "v1",
    team_ids: [],
    discord_state: {},
    ...overrides,
  };
}

export function makeTenantUserPrincipal(
  overrides: Partial<AuthenticatedPrincipalRecord> = {},
): AuthenticatedPrincipalRecord {
  return {
    principal_type: "tenant_user",
    user_id: "user-route25",
    email: "person@example.com",
    full_name: "Person Example",
    memberships: [makeMembership()],
    ...overrides,
  };
}

export function makePlatformAdminPrincipal(
  overrides: Partial<AuthenticatedPrincipalRecord> = {},
): AuthenticatedPrincipalRecord {
  return {
    principal_type: "platform_super_admin",
    username: "admin",
    user_id: null,
    email: null,
    full_name: null,
    memberships: [],
    ...overrides,
  };
}

export function makeDiscordIdentity(
  overrides: Partial<TenantDiscordIdentityRecord> = {},
): TenantDiscordIdentityRecord {
  return {
    linked: false,
    discord_user_id: null,
    discord_username: null,
    discord_global_name: null,
    discord_avatar_hash: null,
    linked_at: null,
    ...overrides,
  };
}

export function makeTeam(overrides: Partial<TenantTeamRecord> = {}): TenantTeamRecord {
  return {
    team_id: "delivery",
    tenant_id: "route25",
    name: "Delivery",
    description: "Delivery team",
    permission_keys: ["analytics.business.view"],
    created_at: "2026-03-27T16:00:00Z",
    updated_at: "2026-03-27T16:00:00Z",
    ...overrides,
  };
}

export function makeInvite(overrides: Partial<TenantInviteRecord> = {}): TenantInviteRecord {
  return {
    invite_id: "invite-route25",
    tenant_id: "route25",
    email: "newperson@example.com",
    full_name: "New Person",
    role: "business_member",
    team_ids: [],
    mode_override: null,
    status: "pending",
    invite_url: "http://localhost:4100/invite/accept?token=invite-token",
    expires_at: "2026-03-29T16:00:00Z",
    accepted_at: null,
    revoked_at: null,
    created_at: "2026-03-27T16:00:00Z",
    updated_at: "2026-03-27T16:00:00Z",
    ...overrides,
  };
}

export function makeMember(overrides: Partial<TenantMemberRecord> = {}): TenantMemberRecord {
  return {
    membership_id: "membership-route25",
    tenant_id: "route25",
    user_id: "user-route25",
    email: "person@example.com",
    full_name: "Person Example",
    is_active: true,
    role: "technical_member",
    permission_keys: ["analytics.business.view", "analytics.technical.view"],
    effective_mode: "technical",
    mode_override: null,
    onboarding_kind: "member_join",
    first_signed_in_at: "2026-03-27T16:00:00Z",
    onboarding_completed_at: "2026-03-27T16:30:00Z",
    onboarding_version: "v1",
    team_ids: [],
    discord_state: {},
    created_at: "2026-03-27T16:00:00Z",
    updated_at: "2026-03-27T16:00:00Z",
    ...overrides,
  };
}

export function makeDeliverySummary(
  overrides: Partial<DeliverySummaryRecord> = {},
): DeliverySummaryRecord {
  return {
    summary: {
      completed_count: 4,
      in_review_count: 1,
      blocked_count: 1,
      failed_count: 0,
      queued_count: 2,
      median_cycle_time_hours: 6,
      average_cycle_time_hours: 8,
    },
    timeline: [
      {
        run_id: "run-1",
        project_id: "route25-default",
        issue_key: "GP-125",
        issue_summary: "Ship onboarding checklist",
        status: "completed",
        completed_at: "2026-03-27T15:00:00Z",
        started_at: "2026-03-27T12:00:00Z",
        pr_url: "https://github.com/thedarkcder/master-builder/pull/173",
      },
    ],
    ...overrides,
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
      project_id: "route25-default",
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
      project_id: "route25-default",
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
  const projects = options.projects ?? [makeProject({ tenant_id: options.run.tenant_id, project_id: options.run.project_id ?? "route25-default" })];
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
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, makePlatformAdminPrincipal()),
    },
    {
      method: "GET",
      pathname: /^\/api\/bff\/api\/admin\/runs\/[^/]+$/,
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
      pathname: /^\/api\/bff\/api\/admin\/runs\/[^/]+\/events$/,
      handler: (route) => fulfillJson(route, options.events ?? []),
    },
    {
      method: "GET",
      pathname: /^\/api\/bff\/api\/admin\/runs\/[^/]+\/logs$/,
      handler: (route) => fulfillJson(route, options.logs ?? []),
    },
    {
      method: "GET",
      pathname: new RegExp(`^/api/bff/api/admin/tenants/${encodeURIComponent(options.run.tenant_id)}/runs/[^/]+/token-timeline$`),
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
      pathname: `/api/bff/api/admin/tenants/${encodeURIComponent(options.run.tenant_id)}`,
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: `/api/bff/api/admin/tenants/${encodeURIComponent(options.run.tenant_id)}/projects`,
      handler: (route) => fulfillJson(route, projects),
    },
    {
      method: "POST",
      pathname: `/api/bff/api/admin/runs/${encodeURIComponent(options.run.run_id)}/rerun`,
      handler: async (route) => {
        const payload = JSON.parse(route.request().postData() ?? "{}") as RunRerunPayload;
        options.onRerun?.(payload);
        await fulfillJson(route, nextRun);
      },
    },
  ]);
}

async function installAuthSessionMock(
  page: Page,
  { principal, userEmail, userName }: TenantSessionSeed,
): Promise<void> {
  let sessionActive = true;
  await page.route(`${APP_BASE_URL}/api/auth/csrf**`, async (route) => {
    await fulfillJson(route, { csrfToken: "playwright-csrf-token" });
  });
  await page.route(`${APP_BASE_URL}/api/auth/signout**`, async (route) => {
    sessionActive = false;
    await fulfillJson(route, { url: `${APP_BASE_URL}/login` });
  });
  await page.route(`${APP_BASE_URL}/api/auth/session**`, async (route) => {
    if (!sessionActive) {
      await fulfillJson(route, null);
      return;
    }
    await fulfillJson(route, {
      user: {
        name: userName ?? principal.full_name ?? principal.username ?? principal.email ?? "Playwright User",
        email: userEmail ?? principal.email ?? null,
        principal,
      },
      expires: "2099-01-01T00:00:00.000Z",
    });
  });
}

async function installApiMocks(page: Page, baseUrl: string, handlers: AppRouteHandler[]): Promise<void> {
  await page.route(`${baseUrl}/**`, async (route) => {
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
    await route.fallback();
  });
}
