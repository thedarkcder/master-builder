import { expect, test } from "@playwright/test";

import {
  fulfillJson,
  installBffApiMocks,
  makeTenant,
  seedAdminSession,
} from "./support/admin-ui";

test("tenant configuration save only calls the configuration endpoint", async ({ page }) => {
  const tenant = makeTenant({
    tenant_id: "example",
    name: "Route 25",
  });
  const updatedTenant = makeTenant({
    ...tenant,
    name: "Route 25 Renamed",
  });
  const calls: string[] = [];

  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/example",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/codex/models",
      handler: (route) =>
        fulfillJson(route, {
          default_model: "gpt-5.4",
          default_reasoning_effort: "medium",
          runtime_kind: "codex_cli",
          profile_name: "engineering_execution",
          models: [],
          reasoning_efforts: [],
        }),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/example/projects",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "PATCH",
      pathname: /\/api\/bff\/api\/admin\/tenants\/example\/(configuration|policy|jira|github|repos|discord|observability)$/,
      handler: async (route, url) => {
        calls.push(url.pathname);
        if (url.pathname.endsWith("/configuration")) {
          const payload = await route.request().postDataJSON();
          expect(payload).toEqual({ name: "Route 25 Renamed" });
          return fulfillJson(route, updatedTenant);
        }
        return fulfillJson(route, { detail: `Unexpected tenant section save: ${url.pathname}` }, 500);
      },
    },
  ]);

  await page.goto("/example/settings/config");
  await expect(page.getByLabel("Tenant enabled")).toHaveCount(0);
  await page.getByPlaceholder("Tenant Demo").fill("Route 25 Renamed");
  await page.getByRole("button", { name: "Save workspace configuration" }).click();

  await expect(page.getByText("Workspace configuration saved")).toBeVisible();
  expect(calls).toEqual(["/api/bff/api/admin/tenants/example/configuration"]);
});

test("tenant policy save only calls the policy endpoint", async ({ page }) => {
  const tenant = makeTenant({
    tenant_id: "example",
    name: "Route 25",
  });
  const updatedTenant = makeTenant({
    ...tenant,
    policy: {
      ...tenant.policy,
      allow_pr_creation: false,
    },
  });
  const calls: string[] = [];

  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/example",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/codex/models",
      handler: (route) =>
        fulfillJson(route, {
          default_model: "gpt-5.4",
          default_reasoning_effort: "medium",
          runtime_kind: "codex_cli",
          profile_name: "engineering_execution",
          models: [],
          reasoning_efforts: [],
        }),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/example/projects",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "PATCH",
      pathname: /\/api\/bff\/api\/admin\/tenants\/example\/(configuration|policy|jira|github|repos|discord|observability)$/,
      handler: async (route, url) => {
        calls.push(url.pathname);
        if (url.pathname.endsWith("/policy")) {
          const payload = await route.request().postDataJSON();
          expect(payload).toMatchObject({
            policy: {
              allow_pr_creation: false,
            },
          });
          return fulfillJson(route, updatedTenant);
        }
        return fulfillJson(route, { detail: `Unexpected tenant section save: ${url.pathname}` }, 500);
      },
    },
  ]);

  await page.goto("/example/settings/config");
  await page.getByLabel("Allow PR creation").uncheck();
  await page.getByRole("button", { name: "Save policy" }).click();

  await expect(page.getByText("Tenant policy saved")).toBeVisible();
  expect(calls).toEqual(["/api/bff/api/admin/tenants/example/policy"]);
});

test("tenant Discord settings load and save through the Discord-only contract", async ({ page }) => {
  const tenant = makeTenant({
    tenant_id: "example",
    name: "Route 25",
    discord: null,
  });
  const updatedTenant = makeTenant({
    ...tenant,
    discord: {
      guild_id: "guild-123",
      onboarding_channel_id: "channel-456",
      onboarding_invite_expires_in_seconds: 3600,
      onboarding_invite_max_uses: 2,
      notify_events: [],
    },
  });
  const calls: string[] = [];
  let codexModelsLoaded = false;

  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/example",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/codex/models",
      handler: (route) => {
        codexModelsLoaded = true;
        return fulfillJson(route, {
          default_model: "gpt-5.4",
          default_reasoning_effort: "medium",
          runtime_kind: "codex_cli",
          profile_name: "engineering_execution",
          models: [],
          reasoning_efforts: [],
        });
      },
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/example/projects",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "PATCH",
      pathname: /\/api\/bff\/api\/admin\/tenants\/example\/(configuration|policy|jira|github|repos|discord|observability)$/,
      handler: async (route, url) => {
        calls.push(url.pathname);
        if (url.pathname.endsWith("/discord")) {
          const payload = await route.request().postDataJSON();
          expect(payload).toEqual({
            discord: {
              guild_id: "guild-123",
              onboarding_channel_id: "channel-456",
              onboarding_invite_expires_in_seconds: 3600,
              onboarding_invite_max_uses: 2,
              notify_events: [],
            },
          });
          return fulfillJson(route, updatedTenant);
        }
        return fulfillJson(route, { detail: `Unexpected tenant section save: ${url.pathname}` }, 500);
      },
    },
  ]);

  await page.goto("/example/settings/discord");
  await expect(page.getByRole("heading", { name: "Discord Integration" })).toBeVisible();
  expect(codexModelsLoaded).toBe(false);

  await page.getByLabel("Enable Discord").check();
  await page.getByPlaceholder("Discord guild/server ID").fill("guild-123");
  await page.getByPlaceholder("Discord channel used for join invites").fill("channel-456");
  await page.getByPlaceholder("86400").fill("3600");
  await page.getByPlaceholder("1").fill("2");
  await page.getByRole("button", { name: "Save Discord settings" }).click();

  await expect(page.getByText("Discord settings saved")).toBeVisible();
  expect(calls).toEqual(["/api/bff/api/admin/tenants/example/discord"]);
});
