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
    execution_id: "wfexec-mab-215",
    workflow_id: "parent_planning:MAB-215",
    tenant_id: "route25",
    project_id: "route25-default",
    issue_key: "MAB-215",
    issue_summary: "Identity and authorization v1 contract",
    orchestration_backend: "temporal",
    dedupe_scope: "parent_planning",
    status: "failed",
    workflow_type: {
      key: "parent_planning",
      label: "Parent Planning",
      description: "Parent planning workflow",
      retry_policy: {
        manual_retry_enabled: true,
        max_attempts: 4,
        initial_interval_seconds: 60,
        max_interval_seconds: 1800,
        backoff_coefficient: 2,
      },
      capabilities: {
        child_issue_links: true,
      },
      lifecycle: {
        state_path_kind: "operation",
        execution_modes: ["fresh", "resume"],
        conditional_paths: ["Human input clarification", "Retry failed operation", "Child issue fanout"],
        states: [
          { key: "running", label: "Running", terminal: false, waits_for_input: false },
          { key: "waiting_for_input", label: "Waiting for input", terminal: false, waits_for_input: true },
          { key: "completed", label: "Completed", terminal: true, waits_for_input: false },
          { key: "failed", label: "Failed", terminal: true, waits_for_input: false },
        ],
        transitions: [
          { from_state: "running", to_state: "waiting_for_input", label: "Ask PM clarification" },
          { from_state: "waiting_for_input", to_state: "running", label: "Resume from answer" },
          { from_state: "running", to_state: "completed", label: "Fan out child work" },
          { from_state: "running", to_state: "failed", label: "Persist operation failure" },
        ],
      },
      operations: [
        {
          operation_type: "jira_child_fanout",
          label: "Fan out engineering child tickets",
          description: "Create or refresh engineering child tickets.",
          completion_required: true,
          status: "failed",
        },
      ],
      orchestration_backend: "legacy",
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
    can_resume: true,
    resume_unavailable_reason: null,
    links: [{ kind: "jira_issue", label: "Jira issue MAB-215", ref: "MAB-215", url: "https://jira.example.test/browse/MAB-215", status: "failed" }],
    failure_reason:
      'Failed to seed Jira issues: Jira API request failed (400): {"errorMessages":["CONTENT_LIMIT_EXCEEDED"],"errors":{}}',
    operations: [
      {
        operation_id: "operation-jira-child-fanout",
        run_id: null,
        operation_type: "jira_child_fanout",
        label: "Fan out engineering child tickets",
        description: "Create or refresh engineering child tickets.",
        required: true,
        definition_only: false,
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
            status_detail: null,
            retryable: true,
            next_retry_at: null,
            started_at: "2026-04-17T12:22:11Z",
            finished_at: "2026-04-17T12:22:11Z",
          },
        ],
        events: [
          {
            event_id: "audit-event-1",
            source: "audit",
            level: "error",
            event_kind: "attempt_failed",
            message:
              'Failed to seed Jira issues: Jira API request failed (400): {"errorMessages":["CONTENT_LIMIT_EXCEEDED"],"errors":{}}',
            source_component: "workflow_operation_service",
            run_id: null,
            operation_id: "operation-jira-child-fanout",
            attempt_id: "attempt-1",
            agent_id: null,
            invocation_id: null,
            stage: null,
            attempt: null,
            stream: null,
            payload: { error_category: "content_limit" },
            recorded_at: "2026-04-17T12:22:11Z",
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
        retry_unavailable_reason: "Latest attempt is not in a failed state.",
        attempts: [
          ...workflow.operations[0].attempts,
          {
            attempt_id: "attempt-2",
            attempt_number: 2,
            status: "running",
            error_category: null,
            error_message: null,
            status_detail: null,
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
  let resumedExecution = false;
  await mockTenantWorkflowApis(page, {
    workflows: [workflow],
    retriedWorkflowResponse: retriedWorkflow,
    telemetryEventsByOperationId: {
      "operation-jira-child-fanout": [
        {
          event_id: "telemetry-stage-request",
          source: "telemetry",
          level: "info",
          event_kind: "stage_request",
          message: "Submitted runtime request.",
          source_component: "runtime_invocation",
          run_id: null,
          operation_id: "operation-jira-child-fanout",
          attempt_id: "attempt-1",
          agent_id: null,
          invocation_id: null,
          stage: null,
          attempt: 1,
          stream: null,
          payload: {
            user_prompt: "Create or refresh engineering child tickets from the parent brief.",
          },
          recorded_at: "2026-04-17T12:23:00Z",
        },
        {
          event_id: "telemetry-jira-request",
          source: "telemetry",
          level: "info",
          event_kind: "jira_child_upsert_request",
          message: "Submitting child Jira issue upsert for Create tenant assurance boundary.",
          source_component: "jira_seed",
          run_id: null,
          operation_id: "operation-jira-child-fanout",
          attempt_id: "attempt-1",
          agent_id: null,
          invocation_id: null,
          stage: null,
          attempt: 1,
          stream: null,
          payload: {
            summary: "Create tenant assurance boundary",
          },
          recorded_at: "2026-04-17T12:23:10Z",
        },
        {
          event_id: "telemetry-outcome",
          source: "telemetry",
          level: "error",
          event_kind: "attempt_failed",
          message:
            'Failed to seed Jira issues: Jira API request failed (400): {"errorMessages":["CONTENT_LIMIT_EXCEEDED"],"errors":{}}',
          source_component: "workflow_operation_attempt",
          run_id: null,
          operation_id: "operation-jira-child-fanout",
          attempt_id: "attempt-1",
          agent_id: null,
          invocation_id: null,
          stage: null,
          attempt: 1,
          stream: null,
          payload: {
            status: "failed",
            attempt_number: 1,
          },
          recorded_at: "2026-04-17T12:23:30Z",
        },
      ],
    },
    auditTranscriptByOperationId: {
      "operation-jira-child-fanout": {
        execution_id: "wfexec-mab-215",
        operation_id: "operation-jira-child-fanout",
        operation_label: "Fan out engineering child tickets",
        current_status: "failed",
        source: "audit",
        attempts: [
          {
            attempt_id: "attempt-1",
            attempt_number: 1,
            status: "failed",
            started_at: "2026-04-17T12:22:11Z",
            finished_at: "2026-04-17T12:23:00Z",
            duration_ms: 49000,
            error_category: "content_limit",
            failure_message:
              'Failed to seed Jira issues: Jira API request failed (400): {"errorMessages":["CONTENT_LIMIT_EXCEEDED"],"errors":{}}',
            status_detail: null,
            recommended_next_action: "Retry engineering child fanout after reducing Jira payload size.",
            sections: [
              {
                kind: "outcome",
                label: "Outcome",
                entries: [
                  {
                    entry_id: "audit-outcome",
                    recorded_at: "2026-04-17T12:23:30Z",
                    level: "error",
                    title: "Attempt failure",
                    message:
                      'Failed to seed Jira issues: Jira API request failed (400): {"errorMessages":["CONTENT_LIMIT_EXCEEDED"],"errors":{}}',
                    source_component: "workflow_operation_service",
                    payload: {
                      error_category: "content_limit",
                    },
                  },
                ],
              },
            ],
          },
        ],
      },
    },
    onResumeExecution: () => {
      resumedExecution = true;
    },
    onRetryOperation: (payload) => {
      retriedOperation = payload;
    },
  });

  await page.goto("/route25/workflows");

  await expect(page.getByRole("link", { name: "Parent Planning" })).toBeVisible();

  await page.getByRole("link", { name: "Parent Planning" }).click();

  await expect(page).toHaveURL(/\/route25\/workflows\/parent_planning$/);
  await expect(page.getByRole("button", { name: "Settings" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Recent executions" })).toBeVisible();
  await page.getByRole("button", { name: "Recent executions" }).click();
  await expect(page.getByRole("button", { name: "Recent executions" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Identity and authorization v1 contract" })).toBeVisible();

  await page.getByRole("link", { name: "Identity and authorization v1 contract" }).click();

  await expect(page).toHaveURL(/\/route25\/executions\/wfexec-mab-215$/);
  await expect(page.getByText("Workflow type")).toBeVisible();
  await expect(page.getByText("Parent Planning")).toBeVisible();
  await expect(page.getByRole("button", { name: "Resume execution" })).toBeEnabled();
  await page.getByRole("button", { name: "Step details" }).click();
  await expect(page.getByRole("heading", { name: "Step details" })).toBeVisible();
  await expect(page.locator("table").getByText("Fan out engineering child tickets").first()).toBeVisible();
  await page.getByRole("button", { name: "Fan out engineering child tickets" }).click();
  await expect(page.getByRole("heading", { name: "Fan out engineering child tickets" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Live telemetry" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Attempt 1/i })).toBeVisible();
  await expect(page.getByText("Attempt 1").last()).toBeVisible();
  await expect(page.getByText("Prompts")).toBeVisible();
  await expect(page.getByText("External requests")).toBeVisible();
  await page.getByRole("button", { name: "Audit history" }).click();
  await expect(page.getByText("Retry engineering child fanout after reducing Jira payload size.").first()).toBeVisible();
  await expect(page.getByText("Outcome")).toBeVisible();
  await expect(
    page.getByText('Failed to seed Jira issues: Jira API request failed (400): {"errorMessages":["CONTENT_LIMIT_EXCEEDED"],"errors":{}}').last(),
  ).toBeVisible();
  await page.getByRole("button", { name: "Close", exact: true }).click();
  const retryStepButton = page.getByRole("button", { name: "Retry step" }).last();
  await retryStepButton.scrollIntoViewIfNeeded();
  await expect(retryStepButton).toBeEnabled();

  await retryStepButton.click();

  expect(retriedOperation).toEqual({
    workflowId: "wfexec-mab-215",
    operationId: "operation-jira-child-fanout",
  });
  await expect(page.getByRole("button", { name: /Attempt 2/i })).toBeVisible();
  await expect(page.getByText("running").last()).toBeVisible();
  expect(resumedExecution).toBe(false);
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await page.getByRole("button", { name: "Links" }).click();
  await expect(page.getByText("Create tenant assurance boundary")).toBeVisible();
  await expect(page.getByText("MAB-300")).toBeVisible();

  await page.getByRole("button", { name: "Execution path" }).click();
  await expect(page.getByRole("button", { name: "Execution path" })).toBeVisible();
  await expect(page.getByText("Fan out engineering child tickets").first()).toBeVisible();
});

test("keeps retry available for missing-input workflow failures", async ({ page }) => {
  await seedAdminSession(page);

  const workflow = makeWorkflow({
    execution_id: "wfexec-mab-clarification",
    workflow_id: "parent_planning:MAB-215",
    tenant_id: "route25",
    project_id: "route25-default",
    issue_key: "MAB-215",
    issue_summary: "Identity and authorization v1 contract",
    orchestration_backend: "temporal",
    dedupe_scope: "parent_planning",
    status: "failed",
    workflow_type: {
      key: "parent_planning",
      label: "Parent Planning",
      description: "Parent planning workflow",
      retry_policy: {
        manual_retry_enabled: true,
        max_attempts: 4,
        initial_interval_seconds: 60,
        max_interval_seconds: 1800,
        backoff_coefficient: 2,
      },
      capabilities: {
        child_issue_links: true,
      },
      lifecycle: {
        state_path_kind: "operation",
        execution_modes: ["fresh", "resume"],
        conditional_paths: ["Human input clarification", "Retry failed operation", "Child issue fanout"],
        states: [
          { key: "running", label: "Running", terminal: false, waits_for_input: false },
          { key: "waiting_for_input", label: "Waiting for input", terminal: false, waits_for_input: true },
          { key: "completed", label: "Completed", terminal: true, waits_for_input: false },
          { key: "failed", label: "Failed", terminal: true, waits_for_input: false },
        ],
        transitions: [
          { from_state: "running", to_state: "waiting_for_input", label: "Ask PM clarification" },
          { from_state: "waiting_for_input", to_state: "running", label: "Resume from answer" },
          { from_state: "running", to_state: "completed", label: "Fan out child work" },
          { from_state: "running", to_state: "failed", label: "Persist operation failure" },
        ],
      },
      operations: [
        {
          operation_type: "jira_child_fanout",
          label: "Fan out engineering child tickets",
          description: "Create or refresh engineering child tickets.",
          completion_required: true,
          status: "failed",
        },
      ],
      orchestration_backend: "legacy",
    },
    current_state: "failed",
    waiting_on: "pm_clarification",
    next_step: "Retry failed operation",
    active_run_id: null,
    latest_checkpoint_id: null,
    latest_checkpoint_kind: null,
    state_path: [
      {
        key: "jira_child_fanout",
        label: "Fan out engineering child tickets",
        status: "failed",
        recorded_at: "2026-04-21T09:22:11Z",
        detail: "Waiting on PM clarification answers.",
      },
    ],
    completed_steps: [],
    failed_steps: ["Fan out engineering child tickets"],
    pending_steps: [],
    retrying_steps: [],
    conditional_branches_taken: [],
    conditional_branches_available: [],
    can_resume: true,
    resume_unavailable_reason: null,
    links: [{ kind: "jira_issue", label: "Jira issue MAB-215", ref: "MAB-215", url: "https://jira.example.test/browse/MAB-215", status: "failed" }],
    failure_reason: "Answer the product clarification on Jira issue MAB-215, then retry engineering child fanout.",
    operations: [
      {
        operation_id: "operation-jira-child-fanout-missing-input",
        run_id: null,
        operation_type: "jira_child_fanout",
        label: "Fan out engineering child tickets",
        description: "Create or refresh engineering child tickets.",
        required: true,
        definition_only: false,
        status: "failed",
        target_system: "jira",
        target_ref: "MAB-215",
        summary: "Answer the product clarification on Jira issue MAB-215, then retry engineering child fanout.",
        can_retry: true,
        retry_unavailable_reason: null,
        events: [],
        attempts: [
          {
            attempt_id: "attempt-missing-input-1",
            attempt_number: 7,
            status: "failed",
            error_category: "missing_input",
            error_message:
              "Answer the product clarification on Jira issue MAB-215, then retry engineering child fanout.\n\nQuestions to answer:\n- What invitation TTL should v1 enforce?",
            status_detail: null,
            retryable: true,
            next_retry_at: null,
            started_at: "2026-04-21T09:22:11Z",
            finished_at: "2026-04-21T09:22:11Z",
          },
        ],
      },
    ],
    runs: [],
    created_at: "2026-04-21T09:22:00Z",
    started_at: "2026-04-21T09:22:11Z",
    finished_at: "2026-04-21T09:22:11Z",
  });

  let retriedOperation: { workflowId: string; operationId: string } | null = null;
  mockTenantWorkflowApis(page, {
    workflows: [workflow],
    retriedWorkflowResponse: makeWorkflow({
      ...workflow,
      status: "running",
      current_state: "running",
      failed_steps: [],
      retrying_steps: ["Fan out engineering child tickets"],
      operations: workflow.operations.map((operation) =>
        operation.operation_id === "operation-jira-child-fanout-missing-input"
          ? {
              ...operation,
              status: "running",
              summary: "Retrying engineering child fanout.",
              attempts: [
                ...(operation.attempts ?? []),
                {
                  attempt_id: "attempt-missing-input-2",
                  attempt_number: 8,
                  status: "running",
                  error_category: null,
                  error_message: null,
                  status_detail: null,
                  retryable: true,
                  next_retry_at: null,
                  started_at: "2026-04-21T10:00:00Z",
                  finished_at: null,
                },
              ],
            }
          : operation
      ),
    }),
    onRetryOperation: (payload) => {
      retriedOperation = payload;
    },
  });

  await page.goto("/route25/executions/wfexec-mab-clarification");
  await page.getByRole("button", { name: "Step details" }).click();

  const retryStepButton = page.getByRole("button", { name: "Retry step" }).last();
  await retryStepButton.scrollIntoViewIfNeeded();
  await expect(retryStepButton).toBeEnabled();
  await expect(page.getByRole("link", { name: "Open Jira issue" }).last()).toBeVisible();

  await retryStepButton.click();

  expect(retriedOperation).toEqual({
    workflowId: "wfexec-mab-clarification",
    operationId: "operation-jira-child-fanout-missing-input",
  });
  await expect(page.getByRole("button", { name: /Attempt 8/i })).toBeVisible();
  await expect(page.getByText("running").last()).toBeVisible();
});
