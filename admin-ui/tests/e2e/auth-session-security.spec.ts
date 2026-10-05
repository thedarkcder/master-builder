import { expect, test, type Page } from "@playwright/test";
import { encode, type JWT } from "next-auth/jwt";

import { getLastWorkspaceCookieName } from "../../lib/workspace-preference";
import { requireAuthSecret } from "../../lib/auth-secret";
import { makeMembership, makePlatformAdminPrincipal, makeTenantUserPrincipal } from "./support/admin-ui";

async function addSignedSession(page: Page, baseURL: string, claims: Record<string, unknown>) {
  // This test signs deliberately malformed payloads; the production validator
  // must reject them after real Auth.js decryption, rather than fixture repair.
  const value = await encode({ secret: requireAuthSecret(), salt: "authjs.session-token", token: claims as JWT, maxAge: 60 });
  await page.context().addCookies([{ name: "authjs.session-token", value, url: baseURL, httpOnly: true, sameSite: "Lax" }]);
}

const bearer = "INVALID_TEST_ACCESS_TOKEN";
const admin = makePlatformAdminPrincipal();

for (const [name, claims] of [
  ["missing principal", { accessToken: bearer }],
  ["null principal", { accessToken: bearer, principal: null }],
  ["unknown principal role", { accessToken: bearer, principal: { ...admin, principal_type: "unknown" } }],
  ["missing backend bearer", { principal: admin }],
  ["empty backend bearer", { principal: admin, accessToken: "" }],
  ["missing identity and bearer", {}],
] as const) {
  test(`signed ${name} session is cleared and cannot acquire platform authority`, async ({ page, baseURL }) => {
    await addSignedSession(page, baseURL!, claims);
    const session = await page.request.get("/api/auth/session");
    expect(session.status()).toBe(200);
    expect(await session.json()).toBeNull();
    expect((await page.context().cookies()).filter(cookie => cookie.name === "authjs.session-token")).toHaveLength(0);

    // Reintroduce the invalid cookie to exercise each independent owned boundary.
    await addSignedSession(page, baseURL!, claims);
    const workflow = await page.request.get("/example-workspace/workflows", { maxRedirects: 0 });
    expect(workflow.status()).toBe(307);
    expect(new URL(workflow.headers().location, baseURL).pathname).toBe("/login");

    await addSignedSession(page, baseURL!, claims);
    const bff = await page.request.get("/api/bff/api/admin/auth/me");
    expect(bff.status()).toBe(401);
    expect(await bff.json()).toEqual({ detail: "Authentication required" });

    await addSignedSession(page, baseURL!, claims);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
    await expect(page).toHaveURL(/\/login$/);
  });
}

for (const [name, principal, destination, workflowDestination] of [
  ["administrator", admin, "/platform/dashboard", null],
  ["tenant member", makeTenantUserPrincipal(), "/example-workspace/dashboard", "/example-workspace/dashboard"],
  ["onboarding tenant", makeTenantUserPrincipal({ memberships: [makeMembership({ onboarding_completed_at: null })] }), "/get-started", "/get-started"],
] as const) {
  test(`valid signed ${name} retains session identity and routing`, async ({ page, baseURL }) => {
    await addSignedSession(page, baseURL!, { principal, accessToken: bearer });
    const session = await page.request.get("/api/auth/session");
    expect(session.status()).toBe(200);
    const payload = await session.json();
    expect(payload.user.principal).toEqual(principal);
    expect(payload.user.accessToken).toBeUndefined();
    await page.goto("/");
    await expect(page).toHaveURL(new RegExp(`${destination}$`));
    const login = await page.request.get("/login", { maxRedirects: 0 });
    expect(login.status()).toBe(307);
    expect(new URL(login.headers().location, baseURL).pathname).toBe(destination);
    const workflow = await page.request.get("/example-workspace/workflows", { maxRedirects: 0 });
    expect(workflow.status()).toBe(workflowDestination ? 307 : 200);
    if (workflowDestination) expect(new URL(workflow.headers().location, baseURL).pathname).toBe(workflowDestination);
  });
}

for (const [preference, destination] of [
  [null, "/tenants/select"],
  ["workspace-two", "/workspace-two/dashboard"],
  ["unrelated-workspace", "/tenants/select"],
] as const) {
  test(`dashboard entry validates multiple-workspace preference ${preference}`, async ({ page, baseURL }) => {
    const principal = makeTenantUserPrincipal({ memberships: [
      makeMembership({ tenant_id: "workspace-one" }),
      makeMembership({ tenant_id: "workspace-two" }),
    ] });
    await addSignedSession(page, baseURL!, { principal, accessToken: bearer });
    if (preference) await page.context().addCookies([{ name: getLastWorkspaceCookieName(), value: preference, url: baseURL! }]);
    await page.goto("/");
    await expect(page).toHaveURL(new RegExp(`${destination}$`));
  });
}
