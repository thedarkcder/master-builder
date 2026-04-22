import { expect, test } from "@playwright/test";

import {
  fulfillJson,
  installBffApiMocks,
  makeProject,
  makeTenant,
  seedAdminSession,
} from "./support/admin-ui";

test("project webhooks allow retrying failed jobs from the queue", async ({ page }) => {
  const tenant = makeTenant({
    tenant_id: "route25",
    name: "Route 25",
  });
  const project = makeProject({
    project_id: "route25-default",
    tenant_id: "route25",
    name: "Route 25 Default",
    github_repository: "thedarkcder/girl-power",
    jira_project_key: "GP",
  });

  let retryRequestSeen = false;
  let webhookFetchCount = 0;

  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects",
      handler: (route) => fulfillJson(route, [project]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects/route25-default",
      handler: (route) => fulfillJson(route, project),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/runs",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects/route25-default/discord/allowlist-requests",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/observability/webhook-jobs",
      handler: (route) => {
        webhookFetchCount += 1;
        const items =
          webhookFetchCount === 1
            ? [
                {
                  job_id: "job-failed-1",
                  transport: "jira_webhook",
                  tenant_id: "route25",
                  project_id: "route25-default",
                  subject_key: "jira:route25:MAB-229",
                  related_run_id: "run-route25-229",
                  dedupe_key: "delivery-1",
                  request_id: "request-1",
                  event_type: "jira:issue_updated",
                  status: "failed",
                  lease_expires_at: null,
                  available_at: "2026-04-22T12:00:00Z",
                  attempt_count: 4,
                  last_error: "Workflow Task in failed state.",
                  created_at: "2026-04-22T12:00:00Z",
                  updated_at: "2026-04-22T12:05:00Z",
                  started_at: "2026-04-22T12:01:00Z",
                  completed_at: "2026-04-22T12:05:00Z",
                },
              ]
            : [
                {
                  job_id: "job-failed-1",
                  transport: "jira_webhook",
                  tenant_id: "route25",
                  project_id: "route25-default",
                  subject_key: "jira:route25:MAB-229",
                  related_run_id: "run-route25-229",
                  dedupe_key: "delivery-1",
                  request_id: "request-1",
                  event_type: "jira:issue_updated",
                  status: "pending",
                  lease_expires_at: null,
                  available_at: "2026-04-22T12:06:00Z",
                  attempt_count: 4,
                  last_error: null,
                  created_at: "2026-04-22T12:00:00Z",
                  updated_at: "2026-04-22T12:06:00Z",
                  started_at: null,
                  completed_at: null,
                },
              ];
        return fulfillJson(route, {
          items,
          total: 1,
          limit: 25,
          offset: 0,
          summary: {
            pending_count: webhookFetchCount === 1 ? 0 : 1,
            processing_count: 0,
            failed_count: webhookFetchCount === 1 ? 1 : 0,
            done_count: 0,
          },
        });
      },
    },
    {
      method: "POST",
      pathname: "/api/bff/api/admin/observability/webhook-jobs/job-failed-1/retry",
      handler: (route) => {
        retryRequestSeen = true;
        return fulfillJson(route, {
          job_id: "job-failed-1",
          transport: "jira_webhook",
          tenant_id: "route25",
          project_id: "route25-default",
          subject_key: "jira:route25:MAB-229",
          related_run_id: "run-route25-229",
          dedupe_key: "delivery-1",
          request_id: "request-1",
          event_type: "jira:issue_updated",
          status: "pending",
          lease_expires_at: null,
          available_at: "2026-04-22T12:06:00Z",
          attempt_count: 4,
          last_error: null,
          created_at: "2026-04-22T12:00:00Z",
          updated_at: "2026-04-22T12:06:00Z",
          started_at: null,
          completed_at: null,
        });
      },
    },
  ]);

  await page.goto("/route25/projects/route25-default/webhooks");

  await expect(page.getByRole("heading", { name: "Webhook Queue" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();

  await page.getByRole("button", { name: "Retry" }).click();

  await expect(page.getByText("Retried webhook job job-failed-1.")).toBeVisible();
  await expect(page.getByText("Pending")).toBeVisible();
  await expect(page.getByText("Workflow Task in failed state.")).toHaveCount(0);
  expect(retryRequestSeen).toBe(true);
});
