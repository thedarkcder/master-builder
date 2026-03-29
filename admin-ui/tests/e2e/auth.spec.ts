import { expect, test } from "@playwright/test";

import {
  fulfillJson,
  installBffApiMocks,
  makePlatformAdminPrincipal,
  makeTenant,
  seedAdminSession,
} from "./support/admin-ui";
import { archiveTenant } from "./support/live-backend";

test("hydrates a stored valid admin session and opens platform admin home", async ({ page }) => {
  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, makePlatformAdminPrincipal()),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants",
      handler: (route) => fulfillJson(route, [makeTenant()]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/runs",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/secrets",
      handler: (route) => fulfillJson(route, []),
    },
  ]);

  await page.goto("/dashboard");

  await expect(page.getByRole("heading", { name: "Operations Overview" })).toBeVisible();
});

test("shows a dedicated Status page for platform services", async ({ page }) => {
  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, makePlatformAdminPrincipal()),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants",
      handler: (route) => fulfillJson(route, [makeTenant()]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/runs",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/secrets",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/status",
      handler: (route) =>
        fulfillJson(route, {
          services: [
            { service_id: "api", label: "API", status: "healthy", summary: "Serving requests", capabilities: [] },
            {
              service_id: "workers",
              label: "Workers",
              status: "healthy",
              summary: "2 workers online",
              capabilities: ["Linux", "macOS"],
              instances: [
                {
                  instance_id: "worker-linux-01",
                  label: "Linux worker",
                  status: "idle",
                  summary: "Idle and ready",
                  updated_at: "2026-03-28T15:30:00.000Z",
                  last_heartbeat_at: "2026-03-28T15:30:00.000Z",
                  capabilities: ["Linux"],
                },
                {
                  instance_id: "worker-mac-01",
                  label: "macOS worker",
                  status: "busy",
                  summary: "Processing a queued run",
                  updated_at: "2026-03-28T15:31:00.000Z",
                  last_heartbeat_at: "2026-03-28T15:31:00.000Z",
                  capabilities: ["macOS"],
                  current_run_id: "run-123",
                },
              ],
            },
            { service_id: "knowledge_jira_sync", label: "Knowledge sync", status: "healthy", summary: "Leader active", capabilities: [] },
            { service_id: "discord_commands", label: "Discord commands", status: "healthy", summary: "Commands synced", capabilities: [] },
          ],
        }),
    },
  ]);

  await page.goto("/dashboard");
  await page.getByRole("link", { name: "Status" }).click();

  await expect(page).toHaveURL(/\/status$/);
  await expect(page.getByRole("heading", { name: "Platform status" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Workers" })).toBeVisible();
  await expect(page.getByText("Worker instances")).toBeVisible();
  await expect(page.getByText("Linux worker")).toBeVisible();
  await expect(page.getByText("macOS worker")).toBeVisible();
  await expect(page.getByText("Idle and ready")).toBeVisible();
  await expect(page.getByText("Processing a queued run")).toBeVisible();
  await expect(page.getByText("Discord commands")).toBeVisible();
});

test("shows a dedicated Agent runtimes page without duplicating platform status sections", async ({ page }) => {
  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, makePlatformAdminPrincipal()),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants",
      handler: (route) => fulfillJson(route, [makeTenant()]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/runs",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/secrets",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/agent-runtimes",
      handler: (route) =>
        fulfillJson(route, {
          role_routing: {
            engineering: "engineering_execution_default",
            review: "engineering_execution_deep",
          },
          name_routing: {
            workflow_review_default: "engineering_execution_deep",
          },
          available_roles: ["pm", "engineering", "test", "review", "marketing"],
          available_named_agents: ["workflow_review_default", "pm_primary"],
          available_profiles: {
            engineering_execution_default: {
              profile_name: "engineering_execution_default",
              runtime_kind: "codex_cli",
              cli_command: "codex",
              model: "gpt-5.4",
              reasoning_effort: "medium",
              tool_bridge_allowed: true,
              fallback_profile: null,
            },
            engineering_execution_deep: {
              profile_name: "engineering_execution_deep",
              runtime_kind: "codex_cli",
              cli_command: "codex",
              model: "gpt-5.4",
              reasoning_effort: "high",
              tool_bridge_allowed: true,
              fallback_profile: null,
            },
            pm_conversation_default: {
              profile_name: "pm_conversation_default",
              runtime_kind: "chat_cli",
              cli_command: "chat",
              model: "gpt-5.4",
              reasoning_effort: "medium",
              tool_bridge_allowed: false,
              fallback_profile: "general_planning_default",
            },
          },
          effective_defaults: {
            role_routing: {
              pm: "pm_conversation_default",
              engineering: "engineering_execution_default",
              test: "engineering_execution_default",
              review: "engineering_execution_default",
              marketing: "pm_conversation_default",
            },
            name_routing: {
              workflow_review_default: "engineering_execution_deep",
              pm_primary: "pm_conversation_default",
            },
          },
        }),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/agent-runtime-profiles",
      handler: (route) =>
        fulfillJson(route, {
          profiles: {
            engineering_execution_default: {
              profile_name: "engineering_execution_default",
              runtime_kind: "codex_cli",
              cli_command: "codex",
              model: "gpt-5.4",
              reasoning_effort: "medium",
              tool_bridge_allowed: true,
              fallback_profile: null,
              base_url: null,
              api_key_secret_ref: null,
              is_builtin: true,
              is_overridden: false,
              can_delete: false,
              can_reset: false,
              usage_references: ["role:engineering"],
            },
            engineering_execution_deep: {
              profile_name: "engineering_execution_deep",
              runtime_kind: "codex_cli",
              cli_command: "codex",
              model: "gpt-5.4",
              reasoning_effort: "high",
              tool_bridge_allowed: true,
              fallback_profile: null,
              base_url: null,
              api_key_secret_ref: null,
              is_builtin: true,
              is_overridden: false,
              can_delete: false,
              can_reset: false,
              usage_references: ["named-agent:workflow_review_default"],
            },
            pm_conversation_default: {
              profile_name: "pm_conversation_default",
              runtime_kind: "chat_cli",
              cli_command: "chat",
              model: "gpt-5.4",
              reasoning_effort: "medium",
              tool_bridge_allowed: false,
              fallback_profile: "general_planning_default",
              base_url: null,
              api_key_secret_ref: null,
              is_builtin: true,
              is_overridden: false,
              can_delete: false,
              can_reset: false,
              usage_references: ["role:pm"],
            },
          },
        }),
    },
  ]);

  await page.goto("/dashboard");
  await page.getByRole("link", { name: "Agent runtimes" }).click();

  await expect(page).toHaveURL(/\/agent-runtimes$/);
  await expect(page.getByRole("heading", { name: "Agent runtimes" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Routing" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Profiles" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Runtime routing" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Role defaults" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Platform status" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Workers" })).toHaveCount(0);
  await expect(page.getByText("Worker instances")).toHaveCount(0);
});

test("redirects unauthenticated access to login for protected routes", async ({ page }) => {
  await page.goto("/tenants/select");

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("keeps the public home page available without redirecting to login", async ({ page }) => {
  await page.goto("/");

  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { name: "Transforming vision into digital reality." })).toBeVisible();
  await expect(page.locator('a[href="/login"]').first()).toBeVisible();
});

test("keeps auth pages linked back to the public home page", async ({ page }) => {
  await page.goto("/login");
  await expect(page.getByRole("link", { name: "Back to home" })).toBeVisible();
  await page.getByRole("link", { name: "Back to home" }).click();
  await expect(page).toHaveURL(/\/$/);

  await page.goto("/register");
  await expect(page.getByRole("link", { name: "Back to home" })).toBeVisible();

  await page.goto("/forgot-password");
  await expect(page.getByRole("link", { name: "Back to home" })).toBeVisible();

  await page.goto("/reset-password?token=sample-token");
  await expect(page.getByRole("link", { name: "Back to home" })).toBeVisible();
});

test("submits the login form and lands on platform admin home", async ({ page }) => {
  await page.goto("/login");

  await page.getByLabel("Email or username").fill("admin");
  await page.getByLabel("Password").fill(process.env.ORCHESTRATOR_ADMIN_PASSWORD ?? "change-me");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/dashboard$/, { timeout: 15000 });
  await expect(page.getByRole("heading", { name: "Operations Overview" })).toBeVisible();
});

test("logging out fully ends the session before another user signs in", async ({ page, request }) => {
  const suffix = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const userTwoEmail = `playwright-auth-two-${suffix}@example.com`;
  const userTwoPassword = "PlaywrightPass456!";
  let tenantId = "";

  try {
    const userTwoResponse = await request.post("http://localhost:4000/api/public/register", {
      data: {
        full_name: "User Two",
        email: userTwoEmail,
        password: userTwoPassword,
        tenant_name: `Auth Workspace Two ${suffix}`,
      },
    });
    expect(userTwoResponse.ok()).toBeTruthy();
    const userTwoRegistration = await userTwoResponse.json();
    tenantId = userTwoRegistration.tenant.tenant_id as string;

    await page.goto("/login");
    await page.getByLabel("Email or username").fill(process.env.ORCHESTRATOR_ADMIN_USERNAME ?? "admin");
    await page.getByLabel("Password").fill(process.env.ORCHESTRATOR_ADMIN_PASSWORD ?? "change-me");
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page).toHaveURL(/\/dashboard$/, { timeout: 15000 });
    await expect(page.getByRole("heading", { name: "Operations Overview" })).toBeVisible();

    await page.getByRole("button", { name: "Logout" }).click();
    await expect(page).toHaveURL(/\/login$/, { timeout: 15000 });

    await page.getByLabel("Email or username").fill(userTwoEmail);
    await page.getByLabel("Password").fill("wrong-password");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/login$/);

    await page.getByLabel("Email or username").fill(userTwoEmail);
    await page.getByLabel("Password").fill(userTwoPassword);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/get-started$/, { timeout: 15000 });
  } finally {
    if (tenantId) {
      await archiveTenant(request, tenantId);
    }
  }
});

test("requests a password reset and completes it through the real browser flow", async ({ page, request }) => {
  const email = `playwright-reset-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
  const oldPassword = "PlaywrightPass123!";
  const newPassword = "PlaywrightPass789!";
  let tenantId = "";

  try {
    const registerResponse = await request.post("http://localhost:4000/api/public/register", {
      data: {
        full_name: "Reset User",
        email,
        password: oldPassword,
        tenant_name: `Reset Workspace ${Date.now()}`,
      },
    });
    expect(registerResponse.ok()).toBeTruthy();
    const registration = await registerResponse.json();
    tenantId = registration.tenant.tenant_id as string;

    await page.goto("/forgot-password");
    await page.getByLabel("Work email").fill(email);
    await page.getByRole("button", { name: "Send reset link" }).click();
    await expect(page.getByText("If an account exists for that email, a reset link has been sent.")).toBeVisible();

    const messageSearch = await request.get(`http://localhost:4206/api/v1/search?query=${encodeURIComponent(email)}`);
    expect(messageSearch.ok()).toBeTruthy();
    const searchPayload = await messageSearch.json();
    expect(Array.isArray(searchPayload.messages)).toBeTruthy();
    const resetMessage = searchPayload.messages.find((message: { Subject?: string }) =>
      String(message.Subject ?? "").includes("Reset your Master Builder password"),
    );
    expect(resetMessage).toBeTruthy();

    const messageResponse = await request.get(`http://localhost:4206/api/v1/message/${resetMessage.ID as string}`);
    expect(messageResponse.ok()).toBeTruthy();
    const messagePayload = await messageResponse.json();
    const textBody = String(messagePayload.Text ?? "");
    const match = textBody.match(/https?:\/\/[^\s]+\/reset-password\?token=[^\s]+/);
    expect(match).toBeTruthy();

    await page.goto(new URL(match![0]).pathname + new URL(match![0]).search);
    await page.getByLabel("New password").fill(newPassword);
    await page.getByRole("button", { name: "Reset password" }).click();
    await expect(page).toHaveURL(/\/login\?reset=success$/, { timeout: 15000 });
    await expect(page.getByText("Password updated. Sign in with your new password.")).toBeVisible();

    await page.getByLabel("Email or username").fill(email);
    await page.getByLabel("Password").fill(oldPassword);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.locator("p[role='alert']")).toContainText("Invalid credentials");

    await page.getByLabel("Password").fill(newPassword);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/get-started$/, { timeout: 15000 });
  } finally {
    if (tenantId) {
      await archiveTenant(request, tenantId);
    }
  }
});
