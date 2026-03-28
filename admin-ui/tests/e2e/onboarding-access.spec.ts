import { expect, test } from "@playwright/test";

import {
  fulfillJson,
  installAppApiMocks,
  installBffApiMocks,
  makeDeliverySummary,
  makeDiscordIdentity,
  makeInvite,
  makeMember,
  makeMembership,
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
  await page.getByRole("button", { name: "Create workspace" }).click();

  await expect(page).toHaveURL(/\/get-started$/);
  await expect(page.getByRole("heading", { name: "Set up workspace" })).toBeVisible();
  await expect(page.getByText("Connect Jira and choose at least one project")).toBeVisible();
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
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/discord/identity",
      handler: (route) => fulfillJson(route, makeDiscordIdentity()),
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
  await expect(page.getByRole("button", { name: "Link Discord" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Open Discord invite" })).toHaveCount(0);
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

  await page.goto("/tenants/route25/analytics/token-overview");

  await expect(page).toHaveURL(/\/tenants\/route25\/analytics\/business$/);
  await expect(page.getByRole("link", { name: "Delivery" })).toBeVisible();
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
          authorize_url: "http://localhost:4100/tenants/new/jira?jira_connection_id=jira-conn-123&jira_oauth=success",
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
          install_url: "http://localhost:4100/tenants/new/github?tenant_id=beta-workspace&github_install=success",
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
          install_url: "http://localhost:4100/tenants/new/discord?tenant_id=beta-workspace&discord_install=success",
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
  await page.getByPlaceholder("Tenant Demo").fill("Beta Workspace");
  await page.getByRole("button", { name: /^Next$/ }).click();

  await expect(page).toHaveURL(/\/tenants\/new\/jira$/);
  await page.getByRole("button", { name: /^Next$/ }).click();
  await expect(page.getByText("Connect Jira before continuing.")).toBeVisible();

  await page.getByRole("button", { name: "Connect Jira" }).click();
  await expect(page).toHaveURL(/jira_connection_id=jira-conn-123/);
  await page.getByRole("button", { name: "Load Jira Projects" }).click();
  await page.getByRole("button", { name: /^Next$/ }).click();

  await expect(page).toHaveURL(/\/tenants\/new\/github$/);
  await page.getByRole("button", { name: "Install GitHub App" }).click();
  await expect(page).toHaveURL(/github_install=success/);
  await page.getByRole("button", { name: /^Next$/ }).click();

  await expect(page).toHaveURL(/\/tenants\/new\/discord$/);
  await page.getByRole("button", { name: /(Reinstall|Install) Discord Bot/ }).click();
  await expect(page).toHaveURL(/discord_install=success/);
  await page.getByLabel("Onboarding channel ID").fill("channel-789");
  await page.getByRole("button", { name: /^Next$/ }).click();

  await expect(page).toHaveURL(/\/tenants\/new\/repos$/);
  await expect(page.locator("select").first()).toContainText("thedarkcder/master-builder");
  await page.getByRole("button", { name: /^Next$/ }).click();

  await expect(page).toHaveURL(/\/tenants\/new\/review$/);
  await page.getByRole("button", { name: "Save Tenant" }).click();
  await expect(page.getByText("Saved tenant beta-workspace.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Open Tenant Editor" })).toBeVisible();
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
  await page.getByRole("link", { name: /Route 25/ }).click();

  await expect(page).toHaveURL(/\/tenants\/route25\/dashboard$/);
  await expect(page.getByRole("heading", { name: "Route 25" })).toBeVisible();
  await expect(page.getByText("Tenant workspace overview.")).toBeVisible();
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

  await page.goto("/tenants/route25/team/members");

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
  await expect(page.getByRole("button", { name: "Workspace administration" }).first()).toBeVisible();
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

  await page.goto(`/tenants/${encodedTenantId}/profile`);

  await expect(page.getByRole("heading", { name: "Profile", exact: true })).toBeVisible();
  await page.getByLabel("Full name").fill("Person Renamed");
  await page.getByLabel("Experience preference").selectOption("non_technical");
  await page.getByRole("button", { name: "Save profile" }).click();
  await expect(page.getByText("Profile updated.")).toBeVisible();

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

  await expect(page.getByText("Discord bot install completed. Confirm the onboarding channel and invite settings.")).toBeVisible();
  await expect(page.getByText("Guild ID:")).toContainText("guild-123");

  await page.getByLabel("Onboarding channel ID").fill("channel-789");
  await page.getByRole("button", { name: /^Next$/ }).click();

  await expect(page).toHaveURL(/\/tenants\/new\/repos$/);
});
