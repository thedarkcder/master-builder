import { expect, test } from "@playwright/test";

import {
  makeRun,
  makeWorkflow,
  mockTenantWorkflowApis,
  seedAdminSession,
} from "./support/admin-ui";

test("shows workflow definitions and retries a failed execution operation", async ({ page }) => {
  await seedAdminSession(page);

  const workflow = makeWorkflow({
    workflow_id: "parent_planning:MAB-215",
    tenant_id: "example",
    project_id: "example-default",
    issue_key: "MAB-215",
    issue_summary: "Identity and authorization v1 contract",
    orchestration_backend: "temporal",
    dedupe_scope: "parent_planning",
    status: "failed",
    workflow_type: {
      key: "parent_planning",
      label: "Parent Planning",
      description: "Parent planning workflow",
      operations: [
        {
          operation_type: "jira_child_fanout",
          label: "Fan out engineering child tickets",
          retry_policy: "Retry transient Jira failures. Fail on permanent Jira validation errors such as content limits.",
          retry_policy_config: {
            manual_retry_enabled: true,
            max_attempts: 4,
            initial_interval_seconds: 60,
            max_interval_seconds: 1800,
            backoff_coefficient: 2,
            non_retryable_error_categories: ["content_limit", "contract_invalid", "authorization_failed"],
          },
          description: "Create or refresh engineering child tickets.",
          required: true,
          status: "failed",
        },
      ],
      orchestration_backend: "legacy",
      engine_config: { temporal: null },
    },
    current_state: "failed",
    waiting_on: null,
    next_step: "Retry failed operation",
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
    links: [{ kind: "jira_issue", label: "Jira issue MAB-215", ref: "MAB-215", url: "https://jira.example.test/browse/MAB-215", status: "failed" }],
    failure_reason:
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
        can_retry: true,
        retry_unavailable_reason: null,
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
    runs: [],
    created_at: "2026-04-17T12:22:11Z",
    started_at: "2026-04-17T12:22:11Z",
    finished_at: null,
  });

  const retriedWorkflow = {
    ...workflow,
    status: "running",
    current_state: "running",
    waiting_on: null,
    next_step: "Create or refresh engineering child tickets.",
    failure_reason: null,
    failed_steps: [],
    pending_steps: ["Fan out engineering child tickets"],
    operations: [
      {
        ...workflow.operations[0],
        status: "running",
        can_retry: false,
        retry_unavailable_reason: "Latest attempt is not marked retryable.",
        attempts: [
          ...workflow.operations[0].attempts,
          {
            attempt_id: "attempt-2",
            attempt_number: 2,
            status: "running",
            error_category: null,
            error_message: null,
            retryable: false,
            next_retry_at: null,
            started_at: "2026-04-17T12:40:00Z",
            finished_at: null,
          },
        ],
      },
    ],
    links: [
      ...workflow.links,
      {
        kind: "child_issue",
        label: "Create tenant assurance boundary",
        ref: "MAB-300",
        url: "https://jira.example.test/browse/MAB-300",
        status: "To Do",
      },
    ],
  };

  let retriedOperation: { workflowId: string; operationId: string } | null = null;
  await mockTenantWorkflowApis(page, {
    workflows: [workflow],
    retriedWorkflowResponse: retriedWorkflow,
    onRetryOperation: (payload) => {
      retriedOperation = payload;
    },
  });

  await page.goto("/example/workflows");

  await expect(page.getByText("Parent Planning")).toBeVisible();
  await expect(page.getByText("Create or refresh engineering child tickets.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Parent Planning" })).toBeVisible();

  await page.getByRole("link", { name: "Parent Planning" }).click();

  await expect(page).toHaveURL(/\/example\/workflows\/parent_planning$/);
  await expect(page.getByText("Execution modes")).toBeVisible();
  await expect(page.getByText("fresh, restart, resume")).toBeVisible();
  await expect(page.getByText("Recent executions")).toBeVisible();
  await expect(page.getByRole("link", { name: "Identity and authorization v1 contract" })).toBeVisible();

  await page.getByRole("link", { name: "Identity and authorization v1 contract" }).click();

  await expect(page).toHaveURL(/\/example\/executions\/parent_planning%3AMAB-215$/);
  await expect(page.getByText("Workflow type")).toBeVisible();
  await expect(page.getByText("Parent Planning")).toBeVisible();
  await expect(page.getByText("Fan out engineering child tickets")).toBeVisible();
  await expect(page.getByText("Execution path")).toBeVisible();
  await expect(page.getByText("Retry failed operation")).toBeVisible();
  await expect(page.getByText("content_limit")).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry operation" })).toBeEnabled();

  await page.getByRole("button", { name: "Retry operation" }).click();

  expect(retriedOperation).toEqual({
    workflowId: "parent_planning:MAB-215",
    operationId: "operation-jira-child-fanout",
  });
  await expect(page.getByText("Retried Fan out engineering child tickets.")).toBeVisible();
  await expect(page.getByText("Create tenant assurance boundary")).toBeVisible();
  await expect(page.getByText("MAB-300")).toBeVisible();
  await expect(page.getByText("Attempt 2")).toBeVisible();
});
