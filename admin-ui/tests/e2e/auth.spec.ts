import { expect, test } from "@playwright/test";

import {
  fulfillJson,
  installBffApiMocks,
  makePlatformAdminPrincipal,
  makeTenant,
  seedAdminSession,
} from "./support/admin-ui";

test("hydrates a stored valid admin session and opens tenant selection", async ({ page }) => {
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
  ]);

  await page.goto("/tenants/select");

  await expect(page.getByRole("heading", { name: "Select a workspace" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Route 25/i })).toBeVisible();
});

test("redirects unauthenticated access to login for protected routes", async ({ page }) => {
  await page.goto("/tenants/select");

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("submits the login form and lands on tenant selection", async ({ page }) => {
  await page.goto("/login");

  await page.getByLabel("Email or username").fill("admin");
  await page.getByLabel("Password").fill(process.env.ORCHESTRATOR_ADMIN_PASSWORD ?? "change-me");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/tenants\/select$/, { timeout: 15000 });
  await expect(page.getByRole("heading", { name: "Select a workspace" })).toBeVisible();
});
