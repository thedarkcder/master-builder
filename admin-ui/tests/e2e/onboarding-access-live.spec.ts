import { expect, test } from "@playwright/test";

function uniqueEmail(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
}

test("registers a tenant admin through the real backend and lands in get started", async ({ page }) => {
  const email = uniqueEmail("playwright-register");

  await page.goto("/register");
  await page.getByLabel("Full name").fill("Playwright Owner");
  await page.getByLabel("Work email").fill(email);
  await page.getByLabel("Workspace name").fill(`Playwright Workspace ${Date.now()}`);
  await page.getByLabel("Password").fill("PlaywrightPass123!");
  await page.getByRole("button", { name: "Create workspace" }).click();

  await expect(page).toHaveURL(/\/get-started$/);
  await expect(page.getByRole("heading", { name: "Set up your workspace before the team starts shipping." })).toBeVisible();
});

test("accepts a real invite through the browser flow and lands in member onboarding", async ({ page, request }) => {
  const ownerEmail = uniqueEmail("playwright-owner");
  const invitedEmail = uniqueEmail("playwright-invite");

  const registerResponse = await request.post("http://localhost:4000/api/public/register", {
    data: {
      full_name: "Playwright Owner",
      email: ownerEmail,
      password: "PlaywrightPass123!",
      tenant_name: `Playwright Invite Workspace ${Date.now()}`,
    },
  });
  expect(registerResponse.ok()).toBeTruthy();
  const registration = await registerResponse.json();
  const tenantId = registration.tenant.tenant_id as string;
  const ownerToken = registration.access_token as string;

  const inviteResponse = await request.post(`http://localhost:4000/api/admin/tenants/${tenantId}/invites`, {
    data: {
      email: invitedEmail,
      full_name: "Playwright Invitee",
      role: "business_member",
      team_ids: [],
      mode_override: "non_technical",
    },
    headers: {
      Authorization: `Bearer ${ownerToken}`,
    },
  });
  expect(inviteResponse.ok()).toBeTruthy();
  const invite = await inviteResponse.json();
  const inviteUrl = new URL(invite.invite_url as string);

  await page.goto(`/invite/accept?${inviteUrl.searchParams.toString()}`);
  await page.getByLabel("Full name").fill("Playwright Invitee");
  await page.getByLabel("Password").fill("PlaywrightPass123!");
  await page.getByRole("button", { name: "Accept invite" }).click();

  await expect(page).toHaveURL(/\/get-started$/);
  await expect(page.getByRole("heading", { name: "Finish your team onboarding." })).toBeVisible();
});

test("shows a visible error for an invalid invite token through the real backend", async ({ page }) => {
  await page.goto("/invite/accept?token=definitely-invalid");
  await page.getByLabel("Full name").fill("Broken Invite");
  await page.getByLabel("Password").fill("PlaywrightPass123!");
  await page.getByRole("button", { name: "Accept invite" }).click();

  await expect(page.locator("p[role='alert']")).toContainText("Invite not found or expired");
});
