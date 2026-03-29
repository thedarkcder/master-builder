import { expect, test } from "@playwright/test";

import {
  APP_BASE_URL,
  fulfillJson,
  installAppApiMocks,
  installBffApiMocks,
  makeDeliverySummary,
  makeDiscordIdentity,
  makeInvite,
  makeMember,
  makeMembership,
  makeProject,
  makePlatformAdminPrincipal,
  makeTeam,
  makeTenant,
  makeTenantUserPrincipal,
  mockCredentialSignIn,
  seedAdminSession,
  seedTenantSession,
} from "./support/admin-ui";

test("registers a tenant admin and redirects into the setup onboarding flow", async ({ page }) => {
  const membership = makeMembership({
    tenant_id: "acme",
    role: "tenant_admin",
    onboarding_kind: "tenant_admin_setup",
    onboarding_completed_at: null,
    first_signed_in_at: null,
  });
  const principal = makeTenantUserPrincipal({
    email: "owner@example.com",
    full_name: "Owner Example",
    memberships: [membership],
  });
  const tenant = makeTenant({
    tenant_id: "acme",
    name: "Acme Workspace",
    github: { installation_id: null, webhook_secret_ref: null },
    jira: { ...makeTenant().jira, connection_id: null, project_keys: [] },
    discord: null,
  });

  await installAppApiMocks(page, [
    {
      method: "POST",
      pathname: "/api/public/register",
      handler: (route) =>
        fulfillJson(route, {
          access_token: "registration-token",
          token_type: "bearer",
          expires_in: 3600,
          principal,
          tenant,
        }),
    },
  ]);
  await mockCredentialSignIn(page, { principal, userEmail: principal.email, userName: principal.full_name });
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, principal),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/acme",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/acme/discord/identity",
      handler: (route) => fulfillJson(route, makeDiscordIdentity()),
    },
  ]);

  await page.goto("/register");

  await page.getByLabel("Full name").fill("Owner Example");
  await page.getByLabel("Work email").fill("owner@example.com");
  await page.getByLabel("Workspace name").fill("Acme Workspace");
  await page.getByLabel("Password").fill("supersecret");
  await Promise.all([
    page.waitForURL(/\/get-started$/, { timeout: 30000 }),
    page.getByRole("button", { name: "Create workspace" }).click(),
  ]);
  await expect(page.getByRole("heading", { name: "Set up workspace" })).toBeVisible();
  await expect(page.getByText("Connect Jira and choose at least one project")).toBeVisible();
  await expect(page.getByText("Credentials")).toHaveCount(0);
  await expect(page.getByText("Open platform secrets")).toHaveCount(0);
  await expect(page.getByText("What this controls")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Finish setup" })).toBeDisabled();
});

test("accepts an invite and lands in the member onboarding flow", async ({ page }) => {
  const membership = makeMembership({
    role: "technical_member",
    effective_mode: "technical",
    onboarding_kind: "member_join",
    onboarding_completed_at: null,
    first_signed_in_at: null,
    team_ids: ["delivery"],
  });
  let principal = makeTenantUserPrincipal({
    email: "designer@example.com",
    full_name: "Design Partner",
    memberships: [membership],
  });
  const tenant = makeTenant({
    tenant_id: "route25",
    discord: {
      guild_id: "guild-123",
      installed_at: "2026-03-27T12:00:00Z",
      onboarding_channel_id: "channel-456",
      onboarding_invite_expires_in_seconds: 86400,
      onboarding_invite_max_uses: 1,
      notify_events: [],
    },
  });

  await installAppApiMocks(page, [
    {
      method: "POST",
      pathname: "/api/public/invites/accept",
      handler: (route) =>
        fulfillJson(route, {
          access_token: "invite-token",
          token_type: "bearer",
          expires_in: 3600,
          principal,
        }),
    },
  ]);
  await mockCredentialSignIn(page, { principal, userEmail: principal.email, userName: principal.full_name });
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, principal),
    },
    {
      method: "PUT",
      pathname: "/api/bff/api/app/tenants/route25/me/settings",
      handler: async (route) => {
        const payload = JSON.parse(route.request().postData() ?? "{}") as { mode_override: "technical" | "non_technical" | null };
        principal = makeTenantUserPrincipal({
          ...principal,
          memberships: [
            makeMembership({
              ...principal.memberships[0],
              mode_override: payload.mode_override,
              effective_mode: payload.mode_override ?? "technical",
            }),
          ],
        });
        await fulfillJson(route, principal);
      },
    },
    {
      method: "POST",
      pathname: "/api/bff/api/admin/tenants/route25/discord/onboarding-invite",
      handler: (route) =>
        fulfillJson(route, {
          invite_url: "https://discord.gg/route25",
          expires_at: "2026-03-29T00:00:00Z",
          max_uses: 1,
        }),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25",
      handler: (route) => fulfillJson(route, tenant),
    },
  ]);

  await page.goto("/invite/accept?token=invite-token");

  await page.getByLabel("Full name").fill("Design Partner");
  await page.getByLabel("Password").fill("supersecret");
  await page.getByRole("button", { name: "Accept invite" }).click();

  await expect(page).toHaveURL(/\/get-started$/);
  await expect(page.getByRole("heading", { name: "Join workspace" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Confirm your details" })).toBeVisible();
  await expect(page.getByText("technical member")).toBeVisible();
  await expect(page.getByText("delivery", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Continue to experience" }).click();
  await expect(page.getByRole("heading", { name: "Select your experience" })).toBeVisible();
  await page.getByRole("button", { name: "Business view" }).click();
  await page.getByRole("button", { name: "Save and continue" }).click();
  await expect(page.getByRole("heading", { name: "Join Discord" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Open Discord" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Continue to finish" })).toHaveCount(0);
  const popupPromise = page.waitForEvent("popup");
  await page.getByRole("button", { name: "Open Discord" }).click();
  const popup = await popupPromise;
  await expect(popup).toHaveURL(/discord(\.gg|\.com\/invite)\/route25/);
  await popup.close();
  await expect(page.getByRole("button", { name: "Continue to finish" })).toBeVisible();
  await page.getByRole("button", { name: "Continue to finish" }).click();
  await expect(page.getByRole("heading", { name: "Finish onboarding" })).toBeVisible();
});

test("redirects non-technical users away from technical analytics surfaces", async ({ page }) => {
  const membership = makeMembership({
    tenant_id: "route25",
    role: "business_member",
    effective_mode: "non_technical",
  });
  const principal = makeTenantUserPrincipal({
    email: "biz@example.com",
    memberships: [membership],
  });
  const tenant = makeTenant({
    tenant_id: "route25",
    experience: { default_mode: "non_technical" },
  });

  await seedTenantSession(page, { principal, userEmail: principal.email, userName: principal.full_name });
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, principal),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/delivery-summary",
      handler: (route) => fulfillJson(route, makeDeliverySummary()),
    },
  ]);

  await page.goto("/route25/analytics/token-overview");

  await expect(page).toHaveURL(/\/route25\/analytics\/business$/, { timeout: 15000 });
  await expect(page.getByRole("link", { name: "Token Overview" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Recent delivery timeline" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Secrets" })).toHaveCount(0);
});

test("lets a platform admin create a workspace through the setup wizard and blocks Jira step validation", async ({ page }) => {
  const principal = makePlatformAdminPrincipal();
  let tenantState = makeTenant({
    tenant_id: "beta-workspace",
    name: "Beta Workspace",
    jira: { ...makeTenant().jira, connection_id: "jira-conn-123", project_keys: ["BETA"] },
    github: { installation_id: "github-install-123", webhook_secret_ref: null },
    repos: { github_repository: "https://github.com/thedarkcder/master-builder" },
    discord: {
      guild_id: "guild-123",
      installed_at: "2026-03-27T11:00:00Z",
      onboarding_channel_id: null,
      onboarding_invite_expires_in_seconds: 86400,
      onboarding_invite_max_uses: 1,
      notify_events: [],
    },
  });

  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, principal),
    },
    {
      method: "POST",
      pathname: "/api/bff/api/admin/jira/connect/start",
      handler: (route) =>
        fulfillJson(route, {
          authorize_url: `${APP_BASE_URL}/tenants/new/jira?jira_connection_id=jira-conn-123&jira_oauth=success`,
          expires_at: "2026-03-29T00:00:00Z",
        }),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/jira/connections/jira-conn-123/projects",
      handler: (route) => fulfillJson(route, [{ key: "BETA", name: "Beta Program" }]),
    },
    {
      method: "POST",
      pathname: "/api/bff/api/admin/tenants",
      handler: (route) => fulfillJson(route, tenantState, 201),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/beta-workspace",
      handler: (route) => fulfillJson(route, tenantState),
    },
    {
      method: "POST",
      pathname: "/api/bff/api/admin/tenants/beta-workspace/github/install/start",
      handler: (route) => {
        tenantState = makeTenant({
          ...tenantState,
          github: { installation_id: "github-install-123", webhook_secret_ref: null },
        });
        return fulfillJson(route, {
          install_url: `${APP_BASE_URL}/tenants/new/github?tenant_id=beta-workspace&github_install=success`,
          expires_at: "2026-03-29T00:00:00Z",
        });
      },
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/beta-workspace/github/repositories",
      handler: (route) =>
        fulfillJson(route, [
          {
            full_name: "thedarkcder/master-builder",
            html_url: "https://github.com/thedarkcder/master-builder",
            default_branch: "main",
            private: true,
          },
        ]),
    },
    {
      method: "POST",
      pathname: "/api/bff/api/admin/tenants/beta-workspace/discord/install/start",
      handler: (route) => {
        tenantState = makeTenant({
          ...tenantState,
          discord: {
            guild_id: "guild-123",
            installed_at: "2026-03-27T11:00:00Z",
            onboarding_channel_id: null,
            onboarding_invite_expires_in_seconds: 86400,
            onboarding_invite_max_uses: 1,
            notify_events: [],
          },
        });
        return fulfillJson(route, {
          install_url: `${APP_BASE_URL}/tenants/new/discord?tenant_id=beta-workspace&discord_install=success`,
          expires_at: "2026-03-29T00:00:00Z",
        });
      },
    },
    {
      method: "PUT",
      pathname: "/api/bff/api/admin/tenants/beta-workspace",
      handler: async (route) => {
        const payload = JSON.parse(route.request().postData() ?? "{}") as {
          name: string;
          repos: { github_repository: string | null };
          discord: {
            onboarding_channel_id?: string | null;
            onboarding_invite_expires_in_seconds?: number | null;
            onboarding_invite_max_uses?: number | null;
          } | null;
        };
        tenantState = makeTenant({
          ...tenantState,
          name: payload.name,
          repos: payload.repos,
          discord: payload.discord
            ? {
                ...tenantState.discord!,
                onboarding_channel_id: payload.discord.onboarding_channel_id ?? null,
                onboarding_invite_expires_in_seconds:
                  payload.discord.onboarding_invite_expires_in_seconds ?? tenantState.discord?.onboarding_invite_expires_in_seconds ?? null,
                onboarding_invite_max_uses:
                  payload.discord.onboarding_invite_max_uses ?? tenantState.discord?.onboarding_invite_max_uses ?? null,
              }
            : null,
        });
        await fulfillJson(route, tenantState);
      },
    },
  ]);

  await page.goto("/tenants/new/basics");
  await page.getByLabel("Workspace name").fill("Beta Workspace");
  await expect(page.getByLabel("Workspace name")).toHaveValue("Beta Workspace");
  await Promise.all([
    page.waitForURL(/\/tenants\/new\/jira$/, { timeout: 15000 }),
    page.getByRole("button", { name: /^Next$/ }).click(),
  ]);

  await page.getByRole("button", { name: /^Next$/ }).click();
  await expect(page).toHaveURL(/\/tenants\/new\/jira$/);
  await expect(page.getByRole("heading", { name: "Connect Jira" })).toBeVisible();

  await page.getByRole("button", { name: "Connect Jira" }).click();
  await expect(page).toHaveURL(/jira_connection_id=jira-conn-123/);
  await page.getByRole("button", { name: "Load projects" }).click();
  await page.getByRole("button", { name: /^Next$/ }).click();

  await expect(page).toHaveURL(/\/tenants\/new\/github$/);
  await page.getByRole("button", { name: "Install GitHub App" }).click();
  await expect(page).toHaveURL(/github_install=success/);
  await page.getByRole("button", { name: /^Next$/ }).click();

  await expect(page).toHaveURL(/\/tenants\/new\/discord$/);
  await page.getByRole("button", { name: /(Reinstall|Install) Discord Bot/ }).click();
  await expect(page).toHaveURL(/discord_install=success/);
  await page.getByLabel("Invite channel").fill("channel-789");
  await page.getByRole("button", { name: /^Next$/ }).click();

  await expect(page).toHaveURL(/\/tenants\/new\/repos$/);
  await expect(page.locator("select").first()).toContainText("thedarkcder/master-builder");
  await page.getByRole("button", { name: /^Next$/ }).click();

  await expect(page).toHaveURL(/\/tenants\/new\/review$/);
  await page.getByRole("button", { name: "Save workspace" }).click();
  await expect(page.getByRole("link", { name: "Open workspace settings" })).toBeVisible();
});

test("opens the tenant workspace from the selector for tenant users", async ({ page }) => {
  const membership = makeMembership({
    tenant_id: "route25",
    role: "business_member",
    onboarding_completed_at: "2026-03-27T16:30:00Z",
    effective_mode: "non_technical",
  });
  const principal = makeTenantUserPrincipal({
    email: "member@example.com",
    memberships: [membership],
  });
  const tenant = makeTenant({
    tenant_id: "route25",
    name: "Route 25",
    experience: { default_mode: "non_technical" },
  });

  await seedTenantSession(page, { principal, userEmail: principal.email, userName: principal.full_name });
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, principal),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants",
      handler: (route) => fulfillJson(route, [tenant]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/delivery-summary",
      handler: (route) => fulfillJson(route, makeDeliverySummary()),
    },
  ]);

  await page.goto("/tenants/select");
  await Promise.all([
    page.waitForURL(/\/route25\/dashboard$/, { timeout: 15000 }),
    page.getByRole("link", { name: /Route 25/ }).click(),
  ]);
  await expect(page.getByRole("heading", { name: "Route 25" })).toBeVisible();
  await expect(page.getByText("Tenant workspace overview.")).toBeVisible();
});

test("lets standard tenant users open Projects without showing project-management actions", async ({ page }) => {
  const membership = makeMembership({
    tenant_id: "route25",
    role: "business_member",
    effective_mode: "non_technical",
    permission_keys: [],
    onboarding_completed_at: "2026-03-27T16:30:00Z",
  });
  const principal = makeTenantUserPrincipal({
    email: "member@example.com",
    full_name: "Member Example",
    memberships: [membership],
  });
  const tenant = makeTenant({
    tenant_id: "route25",
    name: "Route 25",
    experience: { default_mode: "non_technical" },
  });

  await seedTenantSession(page, { principal, userEmail: principal.email, userName: principal.full_name });
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, principal),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects",
      handler: (route) =>
        fulfillJson(route, [
          {
            project_id: "route-web",
            tenant_id: "route25",
            name: "Route Web",
            github_repository: "https://github.com/example/route-web",
            jira_project_key: "WEB",
            is_archived: false,
            policy_overrides: {},
            effective_policy: {
              allow_pr_creation: true,
              allow_jira_transitions: false,
              allow_code_reviews: true,
              allow_pr_remediation: true,
              allow_manual_pr_fix_requests: true,
              allow_label_mutations: true,
              allow_auto_merge: false,
              max_dev_test_review_loops: 2,
              max_pr_auto_remediation_loops: 5,
              max_concurrent_runs: 2,
              allowed_commands: [],
              require_agents_md: false,
              knowledge_base_enabled: true,
              knowledge_auto_answer_mode: "aggressive",
              codex_model: null,
              codex_reasoning_effort: null,
            },
            environment: {},
            secret_refs: {},
            discord: null,
            created_at: "2026-03-27T16:00:00Z",
            updated_at: "2026-03-27T16:00:00Z",
          },
        ]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects/route-web",
      handler: (route) =>
        fulfillJson(route, {
          project_id: "route-web",
          tenant_id: "route25",
          name: "Route Web",
          github_repository: "https://github.com/example/route-web",
          jira_project_key: "WEB",
          is_archived: false,
          policy_overrides: {},
          effective_policy: {
            allow_pr_creation: true,
            allow_jira_transitions: false,
            allow_code_reviews: true,
            allow_pr_remediation: true,
            allow_manual_pr_fix_requests: true,
            allow_label_mutations: true,
            allow_auto_merge: false,
            max_dev_test_review_loops: 2,
            max_pr_auto_remediation_loops: 5,
            max_concurrent_runs: 2,
            allowed_commands: [],
            require_agents_md: false,
            knowledge_base_enabled: true,
            knowledge_auto_answer_mode: "aggressive",
            codex_model: null,
            codex_reasoning_effort: null,
          },
          environment: {},
          secret_refs: {},
          discord: null,
          created_at: "2026-03-27T16:00:00Z",
          updated_at: "2026-03-27T16:00:00Z",
        }),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/runs",
      handler: (route) =>
        fulfillJson(route, { detail: "Admin authentication required" }, 401),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/delivery-summary",
      handler: (route) => fulfillJson(route, makeDeliverySummary()),
    },
  ]);

  await page.goto("/route25/projects");

  await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();
  await expect(page.locator('a[href="/route25/projects/route-web"]').last()).toBeVisible();
  await expect(page.getByRole("link", { name: "All projects" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Add project" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Add project" })).toHaveCount(0);

  await page.locator('a[href="/route25/projects/route-web"]').last().click();
  await expect(page).toHaveURL(/\/route25\/projects\/route-web$/);
  await expect(page.getByText("Project", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Route Web" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open settings" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Add knowledge" })).toHaveCount(0);
  await expect(page.getByText("Failed to load project")).toHaveCount(0);
  await expect(page.getByText("Runs are unavailable: 401: Admin authentication required")).toBeVisible();
});

test("lets platform super admins see team navigation and security controls inside a workspace", async ({ page }) => {
  const principal = makePlatformAdminPrincipal();
  const tenant = makeTenant({
    tenant_id: "route25",
    name: "Route 25",
  });

  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, principal),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects",
      handler: (route) => fulfillJson(route, []),
    },
  ]);

  await page.goto("/route25/profile/security");

  await expect(page.getByRole("link", { name: "Team" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Security" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Update password" })).toBeVisible();
  await expect(page.getByText("Password management for platform administrators")).toHaveCount(0);
});

test("lets tenant admins manage team settings from dedicated Team tabs", async ({ page }) => {
  const membership = makeMembership({
    tenant_id: "route25",
    role: "tenant_admin",
    onboarding_kind: "tenant_admin_setup",
    onboarding_completed_at: "2026-03-27T16:30:00Z",
  });
  const principal = makeTenantUserPrincipal({
    email: "admin@example.com",
    full_name: "Admin Example",
    memberships: [membership],
  });
  const codexCatalog = {
    default_model: "gpt-5.4",
    default_reasoning_effort: "medium",
    models: [{ id: "gpt-5.4", label: "GPT-5.4" }],
    reasoning_efforts: [{ id: "medium", label: "Medium" }],
  };
  const tenantState = {
    tenant: makeTenant({
      tenant_id: "route25",
      experience: { default_mode: "technical" },
      discord: {
        guild_id: "guild-123",
        installed_at: "2026-03-27T11:00:00Z",
        onboarding_channel_id: "channel-456",
        onboarding_invite_expires_in_seconds: 86400,
        onboarding_invite_max_uses: 1,
        notify_events: [],
      },
    }),
    teams: [makeTeam()],
    invites: [makeInvite()],
    members: [
      makeMember({
        role: "business_member",
        effective_mode: "non_technical",
        team_ids: ["delivery"],
        discord_state: { linked: true, guild_joined: true, welcome_status: "sent" },
      }),
    ],
  };

  await seedTenantSession(page, { principal, userEmail: principal.email, userName: principal.full_name });
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, principal),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/codex/models",
      handler: (route) => fulfillJson(route, codexCatalog),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25",
      handler: (route) => fulfillJson(route, tenantState.tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/jira/webhooks/diagnostics",
      handler: (route) =>
        fulfillJson(route, {
          webhook_url: "https://example.test/jira/webhook",
          managed_webhook_ids: [],
          last_received_at: null,
          last_issue_key: null,
          recent_delivery_ok: false,
          recent_delivery_window_minutes: 60,
          last_error: null,
        }),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/members",
      handler: (route) => fulfillJson(route, tenantState.members),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/teams",
      handler: (route) => fulfillJson(route, tenantState.teams),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/invites",
      handler: (route) => fulfillJson(route, { items: tenantState.invites }),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/discord/identity",
      handler: (route) => fulfillJson(route, makeDiscordIdentity({ linked: true, discord_username: "admin-discord" })),
    },
    {
      method: "PUT",
      pathname: "/api/bff/api/admin/tenants/route25",
      handler: async (route) => {
        const payload = JSON.parse(route.request().postData() ?? "{}") as { experience?: { default_mode?: string } };
        tenantState.tenant = makeTenant({
          ...tenantState.tenant,
          experience: { default_mode: payload.experience?.default_mode ?? "technical" },
        });
        await fulfillJson(route, tenantState.tenant);
      },
    },
    {
      method: "POST",
      pathname: "/api/bff/api/admin/tenants/route25/teams",
      handler: async (route) => {
        const payload = JSON.parse(route.request().postData() ?? "{}") as { name: string; description?: string | null; permission_keys: string[] };
        const created = makeTeam({
          team_id: "ops",
          name: payload.name,
          description: payload.description ?? null,
          permission_keys: payload.permission_keys,
        });
        tenantState.teams = [...tenantState.teams, created];
        await fulfillJson(route, created);
      },
    },
    {
      method: "POST",
      pathname: "/api/bff/api/admin/tenants/route25/invites",
      handler: async (route) => {
        const payload = JSON.parse(route.request().postData() ?? "{}") as { email: string; full_name?: string | null; role: string; team_ids: string[]; mode_override: string | null };
        const created = makeInvite({
          invite_id: "invite-2",
          email: payload.email,
          full_name: payload.full_name ?? null,
          role: payload.role,
          team_ids: payload.team_ids,
          mode_override: payload.mode_override as "technical" | "non_technical" | null,
        });
        tenantState.invites = [...tenantState.invites, created];
        await fulfillJson(route, { invite: created });
      },
    },
  ]);

  await page.goto("/route25/team/members");

  await expect(page.getByRole("heading", { name: "Team" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Members" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Teams" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Invites" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Members" })).toBeVisible();

  await expect(page.getByText("Welcome sent")).toBeVisible();

  await page.getByRole("link", { name: "Teams" }).click();
  await expect(page.getByRole("heading", { name: "Teams" })).toBeVisible();
  await expect(page.getByText("Create groups and decide what each team can access.")).toBeVisible();
  await expect(page.getByPlaceholder("Team name")).toHaveCount(0);
  await page.getByRole("button", { name: "New team" }).click();
  await expect(page.getByRole("button", { name: "Manage workspace" }).first()).toBeVisible();
  await expect(page.getByText("tenant.manage")).toHaveCount(0);
  await page.getByPlaceholder("Team name").fill("Ops");
  await page.getByPlaceholder("Description").fill("Ops and enablement");
  await page.getByRole("button", { name: "Create team" }).click();
  await expect(page.getByText("Ops", { exact: true })).toBeVisible();
  await expect(page.getByPlaceholder("Team name")).toHaveCount(0);

  await page.getByRole("link", { name: "Invites" }).click();
  await expect(page.getByRole("heading", { name: "Invites" })).toBeVisible();
  await expect(page.getByText("Invite people to the workspace and choose their access.")).toBeVisible();
  await expect(page.getByText("Team IDs, comma separated")).toHaveCount(0);
  await expect(page.locator('select[aria-label="Role"]')).toContainText("Business member");
  await expect(page.locator('select[aria-label="Experience view"]')).toHaveCount(0);
  await page.getByPlaceholder("Email").fill("newhire@example.com");
  await page.getByPlaceholder("Full name").fill("New Hire");
  await page.getByRole("button", { name: "Delivery" }).click();
  await page.getByRole("button", { name: "Send invite" }).click();
  await expect(page.getByText("newhire@example.com")).toBeVisible();
  await expect(page.getByText("Pending • Business member • Delivery")).toBeVisible();
});

test("redirects platform super admins to the tenant selector after archiving a project", async ({ page }) => {
  const principal = makePlatformAdminPrincipal();
  const tenant = makeTenant({ tenant_id: "route25", name: "Route 25" });
  let project = makeProject({
    project_id: "route-web",
    tenant_id: "route25",
    name: "Route Web",
    github_repository: "https://github.com/example/route-web",
    jira_project_key: "WEB",
    is_archived: false,
  });

  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, principal),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects",
      handler: (route) => fulfillJson(route, project.is_archived ? [] : [project]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects/route-web",
      handler: (route) => fulfillJson(route, project),
    },
    {
      method: "PUT",
      pathname: "/api/bff/api/admin/tenants/route25/projects/route-web",
      handler: async (route) => {
        project = makeProject({
          ...project,
          is_archived: true,
        });
        await fulfillJson(route, project);
      },
    },
  ]);

  await page.goto("/route25/projects/route-web/settings");
  await page.getByRole("button", { name: /Governance/ }).click();
  await page.getByPlaceholder("Route Web").fill("Route Web");
  await page.getByRole("button", { name: "Archive project" }).click();

  await expect(page).toHaveURL(/\/tenants\/select$/, { timeout: 15000 });

  await page.goto("/route25/projects");
  await expect(page.locator('a[href="/route25/projects/route-web"]').last()).toHaveCount(0);
});

test("shows a standalone tenant archive confirmation page and moves the tenant into the archived workspace list", async ({
  page,
}) => {
  const principal = makePlatformAdminPrincipal();
  let archivedTenant = makeTenant({
    tenant_id: "auth-workspace-one-1774668649405-k03pkj",
    name: "Auth Workspace One 1774668649405-k03pkj",
    is_enabled: true,
  });
  const activeTenant = makeTenant({
    tenant_id: "auth-workspace-two-1774668649405-k03pkj",
    name: "Auth Workspace Two 1774668649405-k03pkj",
    is_enabled: true,
  });

  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, principal),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/auth-workspace-one-1774668649405-k03pkj",
      handler: (route) => fulfillJson(route, archivedTenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/codex/models",
      handler: (route) =>
        fulfillJson(route, {
          default_model: "gpt-5.4",
          default_reasoning_effort: "medium",
          models: [{ id: "gpt-5.4", label: "GPT-5.4" }],
          reasoning_efforts: [{ id: "medium", label: "Medium" }],
        }),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/auth-workspace-one-1774668649405-k03pkj/projects",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "POST",
      pathname: "/api/bff/api/admin/tenants/auth-workspace-one-1774668649405-k03pkj/archive",
      handler: async (route) => {
        archivedTenant = makeTenant({
          ...archivedTenant,
          is_enabled: false,
        });
        await fulfillJson(route, archivedTenant);
      },
    },
    {
      method: "POST",
      pathname: "/api/bff/api/admin/tenants/auth-workspace-one-1774668649405-k03pkj/unarchive",
      handler: (route) => fulfillJson(route, archivedTenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants",
      handler: (route) => fulfillJson(route, [archivedTenant, activeTenant]),
    },
  ]);

  page.on("dialog", async (dialog) => {
    await dialog.accept();
  });

  await page.goto("/auth-workspace-one-1774668649405-k03pkj/settings/config");
  await page.getByPlaceholder("Auth Workspace One 1774668649405-k03pkj").fill("Auth Workspace One 1774668649405-k03pkj");
  await page.getByRole("button", { name: "Archive workspace" }).click();

  await expect(page).toHaveURL(/\/auth-workspace-one-1774668649405-k03pkj\/archived/, { timeout: 15000 });
  await expect(page.getByRole("heading", { name: "Workspace archived" })).toBeVisible();
  await page.getByRole("button", { name: "View archived workspaces" }).click();
  await expect(page).toHaveURL(/\/tenants\/select$/, { timeout: 15000 });
  await expect(page.getByText("Active workspaces")).toBeVisible();
  await expect(page.getByText("Archived workspaces", { exact: true })).toBeVisible();
  await expect(page.getByText("auth-workspace-one-1774668649405-k03pkj")).toBeVisible();
  await expect(page.getByText("Auth Workspace Two 1774668649405-k03pkj")).toBeVisible();
});

test("pages the workspace selector when there are many active workspaces", async ({ page }) => {
  const principal = makePlatformAdminPrincipal();
  const tenants = Array.from({ length: 8 }, (_, index) =>
    makeTenant({
      tenant_id: `workspace-${index + 1}`,
      name: `Workspace ${index + 1}`,
      is_enabled: true,
    }),
  );

  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, principal),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants",
      handler: (route) => fulfillJson(route, tenants),
    },
  ]);

  await page.goto("/tenants/select");

  await expect(page.getByText("Workspace 1")).toBeVisible();
  await expect(page.getByText("Workspace 6")).toBeVisible();
  await expect(page.getByText("Workspace 7")).toHaveCount(0);
  await expect(page.getByText("8 active workspaces • Page 1 of 2")).toBeVisible();
  await page.getByRole("button", { name: "Next →" }).click();
  await expect(page.getByText("Workspace 1")).toHaveCount(0);
  await expect(page.getByText("Workspace 7")).toBeVisible();
  await expect(page.getByText("Workspace 8")).toBeVisible();
  await expect(page.getByText("8 active workspaces • Page 2 of 2")).toBeVisible();
});

test("redirects tenant admins to workspace setup after archiving a project", async ({ page }) => {
  const membership = makeMembership({
    tenant_id: "route25",
    role: "tenant_admin",
    permission_keys: ["workspace.manage", "projects.manage", "technical.access"],
    onboarding_kind: "tenant_admin_setup",
    onboarding_completed_at: "2026-03-27T16:30:00Z",
  });
  const principal = makeTenantUserPrincipal({
    email: "owner@example.com",
    full_name: "Owner Example",
    memberships: [membership],
  });
  const tenant = makeTenant({ tenant_id: "route25", name: "Route 25" });
  let project = makeProject({
    project_id: "route-web",
    tenant_id: "route25",
    name: "Route Web",
    github_repository: "https://github.com/example/route-web",
    jira_project_key: "WEB",
    is_archived: false,
  });

  await seedTenantSession(page, { principal, userEmail: principal.email, userName: principal.full_name });
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, principal),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects",
      handler: (route) => fulfillJson(route, project.is_archived ? [] : [project]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects/route-web",
      handler: (route) => fulfillJson(route, project),
    },
    {
      method: "PUT",
      pathname: "/api/bff/api/admin/tenants/route25/projects/route-web",
      handler: async (route) => {
        project = makeProject({
          ...project,
          is_archived: true,
        });
        await fulfillJson(route, project);
      },
    },
  ]);

  await page.goto("/route25/projects/route-web/settings");
  await page.getByRole("button", { name: /Governance/ }).click();
  await page.getByPlaceholder("Route Web").fill("Route Web");
  await page.getByRole("button", { name: "Archive project" }).click();

  await expect(page).toHaveURL(/\/tenants\/new\/basics\?tenant_id=route25$/, { timeout: 15000 });
});

test("hides team navigation for invited users without team-management access", async ({ page }) => {
  const membership = makeMembership({
    tenant_id: "route25",
    role: "business_member",
    effective_mode: "non_technical",
    permission_keys: [],
    onboarding_completed_at: "2026-03-27T16:30:00Z",
  });
  const principal = makeTenantUserPrincipal({
    email: "business@example.com",
    full_name: "Business Example",
    memberships: [membership],
  });
  const tenant = makeTenant({
    tenant_id: "route25",
    experience: { default_mode: "non_technical" },
  });

  await seedTenantSession(page, { principal, userEmail: principal.email, userName: principal.full_name });
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, principal),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/delivery-summary",
      handler: (route) => fulfillJson(route, makeDeliverySummary()),
    },
  ]);

  await page.goto("/route25/dashboard");

  await expect(page.getByRole("heading", { name: "Route 25" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Team" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Profile" })).toBeVisible();
});

test("hides Discord link actions when platform Discord OAuth is unavailable", async ({ page }) => {
  const tenantId = "route25";
  const membership = makeMembership({
    tenant_id: tenantId,
    role: "business_member",
    effective_mode: "non_technical",
    onboarding_completed_at: "2026-03-27T16:30:00Z",
  });
  const principal = makeTenantUserPrincipal({
    email: "invitee@example.com",
    full_name: "Invitee Example",
    memberships: [membership],
  });
  const tenant = makeTenant({
    tenant_id: tenantId,
    experience: { default_mode: "non_technical" },
    discord: {
      guild_id: "guild-123",
      installed_at: "2026-03-27T11:00:00Z",
      onboarding_channel_id: "channel-456",
      onboarding_invite_expires_in_seconds: 86400,
      onboarding_invite_max_uses: 1,
      notify_events: [],
    },
  });

  await seedTenantSession(page, { principal, userEmail: principal.email, userName: principal.full_name });
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, principal),
    },
    {
      method: "GET",
      pathname: `/api/bff/api/admin/tenants/${tenantId}`,
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: `/api/bff/api/admin/tenants/${tenantId}/projects`,
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: `/api/bff/api/admin/tenants/${tenantId}/discord/identity`,
      handler: (route) => fulfillJson(route, makeDiscordIdentity({ oauth_configured: false, linked: false })),
    },
  ]);

  await page.goto(`/${tenantId}/profile`);

  await expect(page.getByRole("heading", { name: "Profile", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Link Discord" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Relink Discord" })).toHaveCount(0);
  await expect(page.getByText("Discord linking is unavailable until platform Discord OAuth is configured.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Copy join link" })).toBeVisible();
});

test("lets a tenant user manage profile details, experience, and password from Profile", async ({ page }) => {
  const tenantId = "route 25";
  const encodedTenantId = encodeURIComponent(tenantId);
  const membership = makeMembership({
    tenant_id: tenantId,
    role: "technical_member",
    effective_mode: "technical",
    mode_override: null,
    onboarding_completed_at: "2026-03-27T16:30:00Z",
  });
  let principal = makeTenantUserPrincipal({
    email: "person@example.com",
    full_name: "Person Example",
    memberships: [membership],
  });
  const tenant = makeTenant({
    tenant_id: tenantId,
    experience: { default_mode: "technical" },
    discord: {
      guild_id: "guild-123",
      installed_at: "2026-03-27T11:00:00Z",
      onboarding_channel_id: "channel-456",
      onboarding_invite_expires_in_seconds: 86400,
      onboarding_invite_max_uses: 1,
      notify_events: [],
    },
  });

  await seedTenantSession(page, { principal, userEmail: principal.email, userName: principal.full_name });
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, principal),
    },
    {
      method: "GET",
      pathname: `/api/bff/api/admin/tenants/${encodedTenantId}`,
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: `/api/bff/api/admin/tenants/${encodedTenantId}/projects`,
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: `/api/bff/api/admin/tenants/${encodedTenantId}/discord/identity`,
      handler: (route) => fulfillJson(route, makeDiscordIdentity({ linked: true, discord_username: "person-discord" })),
    },
    {
      method: "PUT",
      pathname: "/api/bff/api/app/me/profile",
      handler: async (route) => {
        const payload = JSON.parse(route.request().postData() ?? "{}") as { full_name: string };
        principal = makeTenantUserPrincipal({
          ...principal,
          full_name: payload.full_name,
          memberships: [...principal.memberships],
        });
        await fulfillJson(route, principal);
      },
    },
    {
      method: "PUT",
      pathname: `/api/bff/api/app/tenants/${encodedTenantId}/me/settings`,
      handler: async (route) => {
        const payload = JSON.parse(route.request().postData() ?? "{}") as { mode_override: "technical" | "non_technical" | null };
        principal = makeTenantUserPrincipal({
          ...principal,
          memberships: [
            makeMembership({
              ...principal.memberships[0],
              mode_override: payload.mode_override,
              effective_mode: payload.mode_override ?? "technical",
            }),
          ],
        });
        await fulfillJson(route, principal);
      },
    },
    {
      method: "POST",
      pathname: "/api/bff/api/app/me/password",
      handler: async (route) => {
        await fulfillJson(route, principal);
      },
    },
  ]);

  await page.goto(`/${encodedTenantId}/profile`);

  await expect(page.getByRole("heading", { name: "Profile", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Security" })).toBeVisible();
  await page.getByLabel("Full name").fill("Person Renamed");
  await page.getByLabel("Experience preference").selectOption("non_technical");
  await page.getByRole("button", { name: "Save profile" }).click();
  await expect(page.getByText("Profile updated.")).toBeVisible();

  await page.getByRole("link", { name: "Security" }).click();
  await expect(page).toHaveURL(new RegExp(`/${encodedTenantId}/profile/security$`), { timeout: 15000 });
  await expect(page.getByRole("heading", { name: "Security" })).toBeVisible();
  await page.getByLabel("Current password").fill("old-password");
  await page.getByLabel("New password").fill("updated-password");
  await page.getByRole("button", { name: "Update password" }).click();
  await expect(page.getByText("Password updated.")).toBeVisible();
});

test("resumes the wizard on the Discord step after a successful install callback", async ({ page }) => {
  const membership = makeMembership({
    tenant_id: "route25",
    role: "tenant_admin",
    onboarding_kind: "tenant_admin_setup",
    onboarding_completed_at: null,
  });
  const principal = makeTenantUserPrincipal({
    email: "admin@example.com",
    memberships: [membership],
  });
  const tenant = makeTenant({
    tenant_id: "route25",
    github: { installation_id: "github-install", webhook_secret_ref: null },
    jira: { ...makeTenant().jira, connection_id: "jira-connection", project_keys: ["GP"] },
    repos: { github_repository: "thedarkcder/master-builder" },
    discord: {
      guild_id: "guild-123",
      installed_at: "2026-03-27T11:00:00Z",
      onboarding_channel_id: null,
      onboarding_invite_expires_in_seconds: 86400,
      onboarding_invite_max_uses: 1,
      notify_events: [],
    },
  });

  await seedTenantSession(page, { principal, userEmail: principal.email, userName: principal.full_name });
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, principal),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "PUT",
      pathname: "/api/bff/api/admin/tenants/route25",
      handler: async (route) => {
        const payload = JSON.parse(route.request().postData() ?? "{}") as { discord?: { onboarding_channel_id?: string | null } };
        await fulfillJson(
          route,
          makeTenant({
            ...tenant,
            discord: {
              ...tenant.discord!,
              onboarding_channel_id: payload.discord?.onboarding_channel_id ?? null,
            },
          }),
        );
      },
    },
  ]);

  await page.goto("/tenants/new/discord?tenant_id=route25&discord_install=success");

  await expect(page.getByRole("heading", { name: "Install Discord" })).toBeVisible();
  await expect(page.getByText("Workspace ready for Discord install")).toBeVisible();
  await expect(page.getByText("Guild ID:")).toContainText("guild-123");

  await page.getByLabel("Invite channel").fill("channel-789");
  await page.getByRole("button", { name: /^Next$/ }).click();

  await expect(page).toHaveURL(/\/tenants\/new\/repos$/);
});
