import { execFileSync } from "node:child_process";

import type { APIRequestContext } from "@playwright/test";
import { expect, test } from "@playwright/test";

import { archiveTenant, loginTenantUser, seedTenantProject } from "./support/live-backend";

function uniqueEmail(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
}

function seedRunForTenant(tenantId: string, projectId?: string | null): string {
  const runId = `playwright-run-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const databaseUrl =
    process.env.ORCHESTRATOR_DATABASE_URL ??
    process.env.POSTGRES_URL ??
    "postgresql+psycopg://orchestrator:orchestrator@127.0.0.1:4402/orchestrator";
  const psycopgDatabaseUrl = databaseUrl.replace("postgresql+psycopg://", "postgresql://");
  const script = `
import os
from datetime import datetime, timezone
import psycopg

database_url = os.environ["DATABASE_URL"]
run_id = os.environ["RUN_ID"]
tenant_id = os.environ["TENANT_ID"]
project_id = os.environ["PROJECT_ID"]
now = datetime.now(timezone.utc)

with psycopg.connect(database_url) as connection:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into runs (
                run_id,
                tenant_id,
                project_id,
                issue_key,
                issue_summary,
                issue_description,
                repo_url,
                branch,
                pr_url,
                dev_session_id,
                pm_session_id,
                orchestrated_session_id,
                dedupe_scope,
                status,
                last_error,
                plan,
                created_at,
                started_at,
                last_heartbeat_at,
                worker_service_instance_id,
                finished_at
            ) values (
                %(run_id)s,
                %(tenant_id)s,
                %(project_id)s,
                %(issue_key)s,
                %(issue_summary)s,
                %(issue_description)s,
                %(repo_url)s,
                null,
                null,
                null,
                null,
                null,
                'issue_execution',
                'queued',
                null,
                null,
                %(created_at)s,
                null,
                null,
                null,
                null
            )
            """,
            {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "project_id": project_id or None,
                "issue_key": "PW-101",
                "issue_summary": "Playwright pipeline regression",
                "issue_description": "Seeded for invited-user pipeline verification",
                "repo_url": "https://github.com/example/repo",
                "created_at": now,
            },
        )
    connection.commit()
`;
  execFileSync("python3", ["-c", script], {
    env: {
      ...process.env,
      DATABASE_URL: psycopgDatabaseUrl,
      RUN_ID: runId,
      TENANT_ID: tenantId,
      PROJECT_ID: projectId ?? "",
    },
    stdio: "pipe",
  });
  return runId;
}

async function archiveTenantForCredentials(
  request: APIRequestContext,
  email: string,
  password: string,
): Promise<void> {
  const { tenantId } = await loginTenantUser(request, email, password);
  await archiveTenant(request, tenantId);
}

test("registers a tenant admin through the real backend and lands in get started", async ({ page }) => {
  const email = uniqueEmail("playwright-register");
  const password = "PlaywrightPass123!";

  try {
    await page.goto("/register");
    await page.getByLabel("Full name").fill("Playwright Owner");
    await page.getByLabel("Work email").fill(email);
    await page.getByLabel("Workspace name").fill(`Playwright Workspace ${Date.now()}`);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Create workspace" }).click();

    await expect(page).toHaveURL(/\/get-started$/, { timeout: 15000 });
    await expect(page.getByRole("heading", { name: "Set up workspace" })).toBeVisible();
    await expect(page.getByText("Credentials")).toHaveCount(0);
    await expect(page.getByText("Open platform secrets")).toHaveCount(0);
  } finally {
    await archiveTenantForCredentials(page.request, email, password);
  }
});

test("accepts a real invite through the browser flow and lands in member onboarding", async ({ page, request }) => {
  test.setTimeout(60000);
  const ownerEmail = uniqueEmail("playwright-owner");
  const invitedEmail = uniqueEmail("playwright-invite");
  const tenantName = `Playwright Invite Workspace ${Date.now()}`;

  let tenantId = "";
  let ownerToken = "";
  try {
    const registerResponse = await request.post("http://localhost:4000/api/public/register", {
      data: {
        full_name: "Playwright Owner",
        email: ownerEmail,
        password: "PlaywrightPass123!",
        tenant_name: tenantName,
      },
    });
    expect(registerResponse.ok()).toBeTruthy();
    const registration = await registerResponse.json();
    tenantId = registration.tenant.tenant_id as string;
    ownerToken = registration.access_token as string;

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

    await expect(page).toHaveURL(/\/get-started$/, { timeout: 15000 });
    await expect(page.getByRole("heading", { name: "Join workspace" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Confirm your details" })).toBeVisible();
    await page.getByRole("button", { name: "Continue to experience" }).click();
    await expect(page.getByRole("heading", { name: "Select your experience" })).toBeVisible();
    await page.getByRole("button", { name: "Save and continue" }).click();
    await expect(page.getByRole("heading", { name: "Finish onboarding" })).toBeVisible();
    await page.getByRole("button", { name: "Finish" }).click();
    await expect(page).toHaveURL(new RegExp(`/${tenantId}/dashboard$`), { timeout: 30000 });
    await expect(page.getByRole("heading", { name: tenantName })).toBeVisible();
  } finally {
    if (tenantId) {
      await archiveTenant(request, tenantId);
    }
  }
});

test("shows a visible error for an invalid invite token through the real backend", async ({ page }) => {
  await page.goto("/invite/accept?token=definitely-invalid");
  await page.getByLabel("Full name").fill("Broken Invite");
  await page.getByLabel("Password").fill("PlaywrightPass123!");
  await page.getByRole("button", { name: "Accept invite" }).click();

  await expect(page.locator("p[role='alert']")).toContainText("Invite not found or expired");
});

test("lets a tenant admin click Team and load the team workspace", async ({ page, request }) => {
  test.setTimeout(60000);
  const email = uniqueEmail("playwright-team");
  const password = "PlaywrightPass123!";
  const tenantName = `Playwright Team Workspace ${Date.now()}`;
  let tenantId = "";
  let accessToken = "";

  try {
    const registerResponse = await request.post("http://localhost:4000/api/public/register", {
      data: {
        full_name: "Playwright Team Admin",
        email,
        password,
        tenant_name: tenantName,
      },
    });
    expect(registerResponse.ok()).toBeTruthy();
    const registration = await registerResponse.json();
    tenantId = registration.tenant.tenant_id as string;
    accessToken = registration.access_token as string;

    const completeResponse = await request.post(`http://localhost:4000/api/app/onboarding/${tenantId}/complete`, {
      headers: {
        Authorization: `Bearer ${accessToken}`,
      },
    });
    expect(completeResponse.ok()).toBeTruthy();

    await page.goto("/login");
    await page.getByLabel("Email or username").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page).toHaveURL(new RegExp(`/${tenantId}/dashboard$`), { timeout: 20000 });
    await expect(page.getByRole("link", { name: "Team" })).toBeVisible();
    await page.getByRole("link", { name: "Team" }).click();
    await expect(page).toHaveURL(new RegExp(`/${tenantId}/team/members$`), { timeout: 20000 });
    await expect(page.getByRole("heading", { name: "Team" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Members" })).toBeVisible();
  } finally {
    if (tenantId) {
      await archiveTenant(request, tenantId);
    }
  }
});

test("lets a platform super admin click Team inside a tenant workspace", async ({ page, request }) => {
  test.setTimeout(60000);
  const email = uniqueEmail("playwright-platform-team");
  const tenantName = `Playwright Platform Team ${Date.now()}`;
  let tenantId = "";
  let ownerToken = "";

  try {
    const registerResponse = await request.post("http://localhost:4000/api/public/register", {
      data: {
        full_name: "Workspace Owner",
        email,
        password: "PlaywrightPass123!",
        tenant_name: tenantName,
      },
    });
    expect(registerResponse.ok()).toBeTruthy();
    const registration = await registerResponse.json();
    tenantId = registration.tenant.tenant_id as string;
    ownerToken = registration.access_token as string;

    await page.goto("/login");
    await page.getByLabel("Email or username").fill(process.env.ORCHESTRATOR_ADMIN_USERNAME ?? "admin");
    await page.getByLabel("Password").fill(process.env.ORCHESTRATOR_ADMIN_PASSWORD ?? "change-me");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/dashboard$/, { timeout: 20000 });

    await page.goto(`/${tenantId}/dashboard`);
    await expect(page.getByRole("link", { name: "Team" })).toBeVisible();
    await page.getByRole("link", { name: "Team" }).click();
    await expect(page).toHaveURL(new RegExp(`/${tenantId}/team/members$`), { timeout: 20000 });
    await expect(page.getByRole("heading", { name: "Team" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Members" })).toBeVisible();
  } finally {
    if (tenantId) {
      await archiveTenant(request, tenantId);
    }
  }
});

test("lets an invited team member open Pipeline after joining the workspace", async ({ page, request, browser }) => {
  test.setTimeout(120000);
  const ownerEmail = uniqueEmail("playwright-pipeline-owner");
  const invitedEmail = uniqueEmail("playwright-pipeline-member");
  const password = "PlaywrightPass123!";
  const tenantName = `Playwright Pipeline Workspace ${Date.now()}`;
  const teamName = `Delivery ${Date.now()}`;

  let tenantId = "";
  let ownerToken = "";
  const invitedContext = await browser.newContext();
  const invitedPage = await invitedContext.newPage();

  try {
    const registerResponse = await request.post("http://localhost:4000/api/public/register", {
      data: {
        full_name: "Pipeline Owner",
        email: ownerEmail,
        password,
        tenant_name: tenantName,
      },
    });
    expect(registerResponse.ok()).toBeTruthy();
    const registration = await registerResponse.json();
    tenantId = registration.tenant.tenant_id as string;
    ownerToken = registration.access_token as string;

    const completeResponse = await request.post(`http://localhost:4000/api/app/onboarding/${tenantId}/complete`, {
      headers: {
        Authorization: `Bearer ${ownerToken}`,
      },
    });
    expect(completeResponse.ok()).toBeTruthy();

    const defaultProject = seedTenantProject(tenantId, "Delivery Core");
    const seededRunId = seedRunForTenant(tenantId, defaultProject.project_id);

    await page.goto("/login");
    await page.getByLabel("Email or username").fill(ownerEmail);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page).toHaveURL(new RegExp(`/${tenantId}/dashboard$`), { timeout: 20000 });
    await page.getByRole("link", { name: "Team" }).click();
    await expect(page).toHaveURL(new RegExp(`/${tenantId}/team/members$`), { timeout: 20000 });

    await page.getByRole("link", { name: "Teams" }).click();
    await expect(page.getByRole("heading", { name: "Teams" })).toBeVisible();
    await page.getByRole("button", { name: "New team" }).click();
    await page.getByPlaceholder("Team name").fill(teamName);
    await page.getByPlaceholder("Description").fill("Delivery workspace members");
    await page.getByRole("button", { name: "Create team" }).click();
    await expect(page.getByText(teamName, { exact: true })).toBeVisible();

    await page.getByRole("link", { name: "Invites" }).click();
    await expect(page.getByRole("heading", { name: "Invites" })).toBeVisible();
    await page.getByPlaceholder("Email").fill(invitedEmail);
    await page.getByPlaceholder("Full name").fill("Pipeline Member");
    await page.getByRole("button", { name: teamName }).click();
    const inviteResponsePromise = page.waitForResponse((response) =>
      response.request().method() === "POST" &&
      response.url().includes(`/api/bff/api/admin/tenants/${tenantId}/invites`) &&
      response.status() === 201,
    );
    await page.getByRole("button", { name: "Send invite" }).click();
    const inviteResponse = await inviteResponsePromise;
    await expect(page.getByText(invitedEmail)).toBeVisible();
    await expect(page.getByText(`Pending • Business member • ${teamName}`)).toBeVisible();

    const invitePayload = (await inviteResponse.json()) as { invite?: { invite_url?: string | null }; invite_url?: string | null };
    const inviteUrlValue = invitePayload.invite?.invite_url ?? invitePayload.invite_url;
    expect(inviteUrlValue).toBeTruthy();
    const inviteUrl = new URL(inviteUrlValue!);

    await invitedPage.goto(`${inviteUrl.pathname}${inviteUrl.search}`);
    await invitedPage.getByLabel("Full name").fill("Pipeline Member");
    await invitedPage.getByLabel("Password").fill(password);
    await invitedPage.getByRole("button", { name: "Accept invite" }).click();

    await expect(invitedPage).toHaveURL(/\/get-started$/, { timeout: 15000 });
    await expect(invitedPage.getByRole("heading", { name: "Join workspace" })).toBeVisible();
    await invitedPage.getByRole("button", { name: "Continue to experience" }).click();
    await expect(invitedPage.getByRole("heading", { name: "Select your experience" })).toBeVisible();
    await invitedPage.getByRole("button", { name: "Save and continue" }).click();
    await expect(invitedPage.getByRole("heading", { name: "Finish onboarding" })).toBeVisible();
    await invitedPage.getByRole("button", { name: "Finish" }).click();

    await expect(invitedPage).toHaveURL(new RegExp(`/${tenantId}/dashboard$`), { timeout: 30000 });
    await expect(invitedPage.getByRole("heading", { name: tenantName })).toBeVisible();
    await expect(invitedPage.getByText("Connection Status")).toBeVisible();
    await expect(invitedPage.getByRole("link", { name: "Pipeline" })).toBeVisible();
    await expect(invitedPage.getByRole("link", { name: "Team" })).toHaveCount(0);
    await expect(invitedPage.getByRole("link", { name: "All projects" })).toBeVisible();

    await invitedPage.getByRole("link", { name: "All projects" }).click();
    await expect(invitedPage).toHaveURL(new RegExp(`/${tenantId}/projects$`), { timeout: 15000 });
    await expect(invitedPage.getByRole("heading", { name: "Projects" })).toBeVisible();
    await expect(invitedPage.getByText("Insufficient tenant permissions")).toHaveCount(0);
    await invitedPage.goto(`/${tenantId}/projects/${defaultProject.project_id}`);
    await expect(invitedPage.getByRole("heading", { name: defaultProject.name })).toBeVisible();
    await invitedPage.getByRole("tab", { name: "Runs" }).click();
    await expect(invitedPage.getByText("Playwright pipeline regression")).toBeVisible();
    await expect(invitedPage.getByText(seededRunId)).toBeVisible();

    await invitedPage.getByRole("link", { name: "Pipeline" }).click();
    await expect(invitedPage).toHaveURL(new RegExp(`/${tenantId}/runs$`), { timeout: 15000 });
    await expect(invitedPage.getByRole("heading", { name: "Pipeline" })).toBeVisible();
    await expect(invitedPage.getByText("Playwright pipeline regression")).toBeVisible();
    await expect(invitedPage.getByText(seededRunId)).toBeVisible();
    await expect(invitedPage.getByText("Admin authentication required")).toHaveCount(0);
    await expect(invitedPage.getByText("Insufficient tenant permissions")).toHaveCount(0);
  } finally {
    await invitedContext.close();
    if (tenantId) {
      await archiveTenant(request, tenantId);
    }
  }
});
