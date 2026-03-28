import { expect, test } from "@playwright/test";

import {
  fulfillJson,
  installBffApiMocks,
  makePlatformAdminPrincipal,
  makeTenant,
  seedAdminSession,
} from "./support/admin-ui";

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

test("redirects unauthenticated access to login for protected routes", async ({ page }) => {
  await page.goto("/tenants/select");

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
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

  const userTwoResponse = await request.post("http://localhost:4000/api/public/register", {
    data: {
      full_name: "User Two",
      email: userTwoEmail,
      password: "PlaywrightPass456!",
      tenant_name: `Auth Workspace Two ${suffix}`,
    },
  });
  expect(userTwoResponse.ok()).toBeTruthy();

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
  await page.getByLabel("Password").fill("PlaywrightPass456!");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/get-started$/, { timeout: 15000 });
});

test("requests a password reset and completes it through the real browser flow", async ({ page, request }) => {
  const email = `playwright-reset-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
  const oldPassword = "PlaywrightPass123!";
  const newPassword = "PlaywrightPass789!";

  const registerResponse = await request.post("http://localhost:4000/api/public/register", {
    data: {
      full_name: "Reset User",
      email,
      password: oldPassword,
      tenant_name: `Reset Workspace ${Date.now()}`,
    },
  });
  expect(registerResponse.ok()).toBeTruthy();

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
});
