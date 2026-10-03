import { expect, test, type Page } from "@playwright/test";
import { encode } from "next-auth/jwt";

import { requireAuthSecret } from "../../lib/auth-secret";

async function addSignedSession(page: Page, baseURL: string) {
  const token = await encode({
    secret: requireAuthSecret(),
    salt: "authjs.session-token",
    token: {
      sub: "redirect-security-test",
      accessToken: "INVALID_TEST_ACCESS_TOKEN",
      principal: { principal_type: "platform_super_admin", username: "redirect-security-test", memberships: [] },
    },
    maxAge: 60,
  });
  await page.context().addCookies([{ name: "authjs.session-token", value: token, url: baseURL, httpOnly: true, sameSite: "Lax" }]);
}

test("authenticated sign-in rejects backslash authority redirection", async ({ page, baseURL }) => {
  await addSignedSession(page, baseURL!);
  const response = await page.request.get(`/login?next=${encodeURIComponent("/\\external.example")}`, { maxRedirects: 0 });
  expect(response.status()).toBe(400);
  expect(response.headers().location).toBeUndefined();
  expect(await response.text()).toContain("Invalid navigation destination");
});

for (const [name, destination] of [
  ["protocol-relative", "//external.example"],
  ["absolute", "https://external.example/"],
  ["newline", "/\n/external.example"],
  ["encoded backslash", "/%5cexternal.example"],
  ["encoded authority", "/%2fexternal.example"],
  ["normalized authority", "/local/..//external.example"],
  ["empty", ""],
] as const) {
  for (const entry of ["login", "register"]) {
    test(`signed ${entry} rejects ${name} destinations`, async ({ page, baseURL }) => {
      await addSignedSession(page, baseURL!);
      const response = await page.request.get(`/${entry}?next=${encodeURIComponent(destination)}`, { maxRedirects: 0 });
      expect(response.status()).toBe(400);
      expect(response.headers().location).toBeUndefined();
      expect(await response.text()).toContain("Invalid navigation destination");
    });
  }
}

test("invalid anonymous sign-in destination is an explicit UI error", async ({ page }) => {
  await page.goto(`/login?next=${encodeURIComponent("/\\external.example")}`);
  await expect(page.getByRole("alert").filter({ hasText: "Invalid navigation destination" })).toContainText("Invalid navigation destination");
  await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeDisabled();
  const authenticationRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path === "/api/auth/providers" || path === "/api/auth/csrf" || path === "/api/auth/callback/credentials") {
      authenticationRequests.push(path);
    }
  });
  await page.getByLabel("Email or username").fill("redirect-security-test");
  await page.getByLabel("Password", { exact: true }).fill("INVALID_TEST_PASSWORD");
  await page.getByLabel("Password", { exact: true }).press("Enter");
  // Also exercise the real form handler without relying on its disabled button.
  await page.locator("form").evaluate((form) => (form as HTMLFormElement).requestSubmit());
  await expect(page.getByRole("alert").filter({ hasText: "Invalid navigation destination" })).toBeVisible();
  expect(authenticationRequests).toEqual([]);
  await expect(page).toHaveURL(/\/login\?next=/);
});

test("signed login retains local query and fragment", async ({ page, baseURL }) => {
  await addSignedSession(page, baseURL!);
  const response = await page.request.get(`/login?next=${encodeURIComponent("/privacy?from=login#policy")}`, { maxRedirects: 0 });
  expect(response.status()).toBe(307);
  const destination = new URL(response.headers().location, response.url());
  expect(destination.href).toBe(`${baseURL}/privacy?from=login#policy`);
  expect(destination.origin).toBe(new URL(baseURL!).origin);
  await page.goto(destination.href);
  await expect(page).toHaveURL(`${baseURL}/privacy?from=login#policy`);
  await expect(page.getByRole("heading", { name: "Privacy Policy", exact: true })).toBeVisible();
});

test("signed login without next keeps the existing platform route", async ({ page, baseURL }) => {
  await addSignedSession(page, baseURL!);
  const response = await page.request.get("/login", { maxRedirects: 0 });
  expect(response.status()).toBe(307);
  const destination = new URL(response.headers().location, response.url());
  expect(destination.href).toBe(`${baseURL}/platform/dashboard`);
  expect(destination.origin).toBe(new URL(baseURL!).origin);
});


test("archived workspace refuses an external continuation link", async ({ page, baseURL }) => {
  await addSignedSession(page, baseURL!);
  await page.goto(`/example-workspace/archived?next=${encodeURIComponent("//external.example")}`);
  await expect(page.getByRole("heading", { name: "Workspace archived", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "View archived workspaces", exact: true })).toHaveCount(0);
  await expect(page.getByRole("alert").filter({ hasText: "Invalid navigation destination" })).toBeVisible();
});


test("archived continuation retains a canonical local query and fragment", async ({ page, baseURL }) => {
  await addSignedSession(page, baseURL!);
  await page.goto(`/example-workspace/archived?next=${encodeURIComponent("/local/../privacy?from=archived#policy")}`);
  const continuation = page.getByRole("link", { name: "View archived workspaces", exact: true });
  await expect(continuation).toHaveAttribute("href", "/privacy?from=archived#policy");
  await continuation.click();
  await expect(page).toHaveURL(`${baseURL}/privacy?from=archived#policy`);
  await expect(page.getByRole("heading", { name: "Privacy Policy", exact: true })).toBeVisible();
});

test("archived continuation without next keeps its workspace selector", async ({ page, baseURL }) => {
  await addSignedSession(page, baseURL!);
  await page.goto("/example-workspace/archived");
  await expect(page.getByRole("link", { name: "View archived workspaces", exact: true })).toHaveAttribute("href", "/tenants/select");
});
