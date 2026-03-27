import { expect, test } from "@playwright/test";

import { AUTH_STORAGE_KEY } from "../../lib/auth-constants";
import {
  fulfillJson,
  installAdminApiMocks,
  seedAdminSession,
} from "./support/admin-ui";

test("hydrates a stored valid admin session and opens tenant selection", async ({ page }) => {
  await seedAdminSession(page);
  await installAdminApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/admin/auth/me",
      handler: (route) => fulfillJson(route, { username: "admin" }),
    },
    {
      method: "GET",
      pathname: "/api/admin/tenants",
      handler: (route) =>
        fulfillJson(route, [
          {
            tenant_id: "route25",
            name: "Route 25",
            is_enabled: true,
            jira: {
              connection_id: null,
              project_keys: ["GP"],
              ready_statuses: ["Ready"],
              ready_jql: null,
              ready_label: "ready",
              in_progress_label: "in-progress",
              blocked_label: "blocked",
              done_label: "done",
              webhook_secret_ref: null,
            },
            github: { webhook_secret_ref: null, installation_id: null },
            repos: { github_repository: "thedarkcder/girl-power" },
            policy: {
              allow_jira_transitions: true,
              allow_pr_creation: true,
              allow_code_reviews: true,
              allow_pr_remediation: true,
              allow_manual_pr_fix_requests: true,
              allow_label_mutations: true,
              allow_auto_merge: false,
              max_runtime_minutes: 120,
              max_dev_test_review_loops: 3,
              max_pr_auto_remediation_loops: 2,
              max_concurrent_runs: 2,
              allowed_commands: [],
              require_agents_md: false,
              knowledge_base_enabled: false,
              knowledge_auto_answer_mode: "safe",
            },
            discord: null,
            created_at: "2026-03-27T16:00:00Z",
            updated_at: "2026-03-27T16:00:00Z",
          },
        ]),
    },
  ]);

  await page.goto("/tenants/select");

  await expect(page.getByRole("heading", { name: "Select a workspace" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Route 25/i })).toBeVisible();
});

test("clears an invalid stored session and redirects protected routes back to login", async ({ page }) => {
  await seedAdminSession(page, "expired-token");
  await installAdminApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/admin/auth/me",
      handler: (route) =>
        route.fulfill({
          status: 401,
          contentType: "application/json",
          body: JSON.stringify({ detail: "Invalid token" }),
        }),
    },
  ]);

  await page.goto("/tenants/select");

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Admin sign in" })).toBeVisible();
  await expect(page.evaluate((storageKey) => window.localStorage.getItem(storageKey), AUTH_STORAGE_KEY)).resolves.toBeNull();
});

test("submits the login form and lands on the dashboard", async ({ page }) => {
  await installAdminApiMocks(page, [
    {
      method: "POST",
      pathname: "/api/admin/auth/login",
      handler: (route) => fulfillJson(route, { access_token: "fresh-token" }),
    },
    {
      method: "GET",
      pathname: "/api/admin/auth/me",
      handler: (route) => fulfillJson(route, { username: "admin" }),
    },
    {
      method: "GET",
      pathname: "/api/admin/tenants",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/admin/runs",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/admin/secrets",
      handler: (route) => fulfillJson(route, []),
    },
  ]);

  await page.goto("/login");

  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("secret");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole("heading", { name: "Operations Overview" })).toBeVisible();
});
