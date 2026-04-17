import { expect, test } from "@playwright/test";

import {
  makeRun,
  makeWorkflow,
  mockTenantWorkflowApis,
  seedAdminSession,
} from "./support/admin-ui";

test("shows blocked standalone workflows and retries them into a fresh run", async ({ page }) => {
  await seedAdminSession(page);

  const workflow = makeWorkflow({
    workflow_id: "legacy-parent-planning:MAB-215",
    tenant_id: "example",
    project_id: "example-default",
    issue_key: "MAB-215",
    issue_summary: "Identity and authorization v1 contract",
    orchestration_backend: "temporal",
    dedupe_scope: "parent_planning",
    status: "blocked",
    workflow_type: {
      key: "legacy-parent-planning",
      label: "Parent Planning",
      description: "Parent planning workflow",
      operations: [
        {
          operation_type: "jira_child_fanout",
          label: "Fan out engineering child tickets",
          retry_policy: "Retry transient Jira failures. Block on permanent Jira validation errors such as content limits.",
          description: "Create or refresh engineering child tickets.",
          required: true,
          status: "failed",
        },
      ],
    },
    current_state: "blocked",
    waiting_on: "operator_remediation",
    next_step: "Resolve blocker",
    active_run_id: null,
    latest_checkpoint_id: null,
    latest_checkpoint_kind: null,
    state_path: [
      {
        key: "jira_child_fanout",
        label: "Fan out engineering child tickets",
        status: "failed",
        recorded_at: "2026-04-17T12:22:11Z",
        detail: "Jira fanout failed on content limit.",
      },
    ],
    completed_steps: [],
    failed_steps: ["Fan out engineering child tickets"],
    pending_steps: [],
    retrying_steps: [],
    conditional_branches_taken: [],
    conditional_branches_available: [],
    available_actions: [],
    links: [{ kind: "jira_issue", label: "Jira issue MAB-215", ref: "MAB-215", url: "https://jira.example.test/browse/MAB-215", status: "blocked" }],
    blocked_reason:
      'Failed to seed Jira issues: Jira API request failed (400): {"errorMessages":["CONTENT_LIMIT_EXCEEDED"],"errors":{}}',
    operations: [
      {
        operation_id: "operation-jira-child-fanout",
        run_id: null,
        operation_type: "jira_child_fanout",
        status: "failed",
        target_system: "jira",
        target_ref: "MAB-215",
        summary:
          'Failed to seed Jira issues: Jira API request failed (400): {"errorMessages":["CONTENT_LIMIT_EXCEEDED"],"errors":{}}',
        blocker_id: "blocker-content-limit",
        attempts: [
          {
            attempt_id: "attempt-1",
            attempt_number: 1,
            status: "failed",
            error_category: "content_limit",
            error_message:
              'Failed to seed Jira issues: Jira API request failed (400): {"errorMessages":["CONTENT_LIMIT_EXCEEDED"],"errors":{}}',
            retryable: true,
            next_retry_at: null,
            started_at: "2026-04-17T12:22:11Z",
            finished_at: "2026-04-17T12:22:11Z",
          },
        ],
      },
    ],
    blockers: [
      {
        blocker_id: "blocker-content-limit",
        operation_id: "operation-jira-child-fanout",
        category: "content_limit",
        message:
          'Failed to seed Jira issues: Jira API request failed (400): {"errorMessages":["CONTENT_LIMIT_EXCEEDED"],"errors":{}}',
        status: "open",
        created_at: "2026-04-17T12:22:11Z",
        resolved_at: null,
      },
    ],
    runs: [],
    created_at: "2026-04-17T12:22:11Z",
    started_at: "2026-04-17T12:22:11Z",
    finished_at: null,
  });

  const nextRun = makeRun({
    run_id: "retry-run-215",
    workflow_id: "legacy-parent-planning:MAB-215-retry",
    tenant_id: "example",
    project_id: "example-default",
    issue_key: "MAB-215",
    issue_summary: "Identity and authorization v1 contract",
    created_at: "2026-04-17T12:40:00Z",
    started_at: null,
    finished_at: null,
    status: "queued",
    pr_url: null,
  });

  let retryPayload: { mode: string } | null = null;
  await mockTenantWorkflowApis(page, {
    workflows: [workflow],
    nextAttemptResponse: nextRun,
    onCreateAttempt: (payload) => {
      retryPayload = payload;
    },
  });

  await page.goto("/example/workflows");

  await expect(page.getByRole("columnheader", { name: "Execution" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Workflow type" })).toBeVisible();
  await expect(page.getByText("MAB-215")).toBeVisible();
  await expect(page.getByText("Parent Planning")).toBeVisible();
  await expect(page.getByText("CONTENT_LIMIT_EXCEEDED")).toBeVisible();
  await expect(page.getByRole("link", { name: "Identity and authorization v1 contract" })).toBeVisible();

  await page.getByRole("link", { name: "Identity and authorization v1 contract" }).click();

  await expect(page).toHaveURL(/\/example\/workflows\/legacy-parent-planning%3AMAB-215$/);
  await expect(page.getByText("Workflow type")).toBeVisible();
  await expect(page.getByText("Parent Planning")).toBeVisible();
  await expect(page.getByText("Fan out engineering child tickets")).toBeVisible();
  await expect(page.getByText("Execution path")).toBeVisible();
  await expect(page.getByText("Resolve blocker")).toBeVisible();
  await expect(page.getByText("content_limit")).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry execution" })).toBeEnabled();

  await Promise.all([
    page.waitForURL(/\/example\/runs\/retry-run-215$/, { timeout: 15000 }),
    page.getByRole("button", { name: "Retry execution" }).click(),
  ]);

  expect(retryPayload).toEqual({ mode: "fresh" });
  await expect(page.getByText("retry-run-215")).toBeVisible();
});
