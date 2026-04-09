import { expect, test } from "@playwright/test";

import {
  makeExecutionSnapshotPlan,
  makeRun,
  makeStageInvocationLogs,
  mockRunDetailApis,
  seedAdminSession,
} from "./support/admin-ui";

test("renders checkpoint-backed failed-after-dev runs with separate execution and integration branches", async ({ page }) => {
  const run = makeRun({
    plan: makeExecutionSnapshotPlan({
      stages: {
        pm: {
          status: "completed",
          completed_at: "2026-03-27T16:55:00Z",
          summary: "PM plan captured.",
        },
        dev: {
          status: "completed",
          completed_at: "2026-03-27T17:02:57Z",
          summary: "PR created and code pushed.",
        },
      },
      execution_context: {
        integration_branch: "release/2026-03-27",
        execution_branch: "run/gp-124/5de2cedf-b7ae-400c-a53c-3beecf078a51",
        base_branch: "main",
        execution_repo_dir: "/tmp/worktree",
      },
    }),
  });
  const logs = makeStageInvocationLogs({
    stage: "dev",
    invocationId: "dev-invocation-1",
    command: "stage.dev",
    startedAt: "2026-03-27T17:00:00Z",
    finishedAt: "2026-03-27T17:02:57Z",
    codexSessionId: "019d1c32-5b72-7c53-bad0-8be1f842b1c2",
  });

  await seedAdminSession(page);
  await mockRunDetailApis(page, { run, logs });

  await page.goto(`/runs/${run.run_id}`);

  await expect(page.getByText("Loading run details...")).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByText("Not active")).toBeVisible();
  await expect(page.getByTestId("run-branch")).toContainText("feature/GP-124");
  await expect(page.getByTestId("run-integration-branch")).toContainText("release/2026-03-27");
  await expect(page.getByTestId("run-execution-branch")).toContainText(`run/gp-124/${run.run_id}`);

  await expect(page.getByTestId("run-stage-dev")).toHaveAttribute("data-stage-status", "completed");
  await expect(page.getByTestId("run-stage-dev-detail")).toHaveText("1m 0s");
  await expect(page.getByTestId("run-stage-test")).toHaveAttribute("data-stage-status", "not_started");
  await expect(page.getByTestId("run-stage-test-detail")).toContainText("not started");
  await expect(page.getByTestId("run-stage-review")).toHaveAttribute("data-stage-status", "not_started");
  await expect(page.getByRole("heading", { name: "Failure Reason" })).toBeVisible();
  await expect(page.getByText(/heartbeat timeout/i)).toBeVisible();

  await page.getByRole("button", { name: "Agents" }).click();
  await expect(page.getByText("PR created and code pushed.", { exact: true })).toBeVisible({ timeout: 15000 });
});

test("marks a finished stage without a checkpoint as interrupted on terminal runs", async ({ page }) => {
  const run = makeRun({
    plan: makeExecutionSnapshotPlan({
      stages: {
        pm: {
          status: "completed",
          completed_at: "2026-03-27T16:55:00Z",
          summary: "PM plan captured.",
        },
      },
      execution_context: {
        integration_branch: "feature/GP-124",
        execution_branch: "run/gp-124/5de2cedf-b7ae-400c-a53c-3beecf078a51",
      },
    }),
  });
  const logs = makeStageInvocationLogs({
    stage: "dev",
    invocationId: "dev-invocation-1",
    command: "stage.dev",
    startedAt: "2026-03-27T17:00:00Z",
    finishedAt: "2026-03-27T17:02:57Z",
    codexSessionId: "019d1c32-5b72-7c53-bad0-8be1f842b1c2",
  });

  await seedAdminSession(page);
  await mockRunDetailApis(page, { run, logs });

  await page.goto(`/runs/${run.run_id}`);

  await expect(page.getByText("Loading run details...")).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByTestId("run-stage-dev")).toHaveAttribute("data-stage-status", "interrupted");
  await expect(page.getByTestId("run-stage-dev-detail")).toHaveText("interrupted");
  await expect(page.getByTestId("run-stage-test")).toHaveAttribute("data-stage-status", "not_started");

  await page.getByRole("button", { name: "Agents" }).click();
  await expect(page.getByText("agent finished, checkpoint missing")).toBeVisible({ timeout: 15000 });
});

test("offers execution resume when review state exists and posts the workflow attempt payload", async ({ page }) => {
  const run = makeRun({
    plan: makeExecutionSnapshotPlan({
      stages: {
        pm: {
          status: "completed",
          completed_at: "2026-03-27T16:55:00Z",
          summary: "PM plan captured.",
          artifact: {
            plan_steps: ["Define implementation scope."],
            acceptance_criteria: ["AC 1"],
            risks: [],
            outcome: "continue",
            next_stage: "dev",
            execution_worker_capability: "linux",
            resolved_prerequisites: [],
            unresolved_prerequisites: [],
          },
        },
        dev: {
          status: "completed",
          completed_at: "2026-03-27T17:02:57Z",
          summary: "Code pushed.",
          artifact: {
            change_summary: ["Implemented fix."],
            pr_url: "https://github.com/thedarkcder/girl-power/pull/21",
            outcome: "continue",
          },
        },
        review: {
          status: "completed",
          completed_at: "2026-03-27T17:07:00Z",
          summary: "Review found follow-up items.",
          artifact: {
            summary: ["Address latest review feedback."],
            outcome: "continue",
            feedback: "Address latest review feedback.",
            pr_url: "https://github.com/thedarkcder/girl-power/pull/21",
          },
        },
      },
      execution_context: {
        integration_branch: "feature/GP-124",
        execution_branch: "run/gp-124/5de2cedf-b7ae-400c-a53c-3beecf078a51",
      },
    }),
  });
  let rerunPayload: unknown = null;

  await seedAdminSession(page);
  await mockRunDetailApis(page, {
    run,
    onCreateAttempt: (payload) => {
      rerunPayload = payload;
    },
  });

  await page.goto(`/runs/${run.run_id}`);

  await expect(page.getByText("Loading run details...")).toHaveCount(0, { timeout: 15000 });
  await page.getByTestId("run-rerun-trigger").click();
  await expect(page.getByTestId("rerun-option-fresh")).toBeVisible();
  await expect(page.getByTestId("rerun-option-execution")).toBeVisible();
  await page.getByTestId("rerun-option-execution").click();

  expect(rerunPayload).toEqual({ mode: "resume", checkpoint_kind: "execution" });
  await expect(page).toHaveURL(/8e8957f2-79f8-4dc8-8deb-786b2c93828d$/, { timeout: 15000 });
});

test("offers start from the start as a fresh rerun with no checkpoint payload", async ({ page }) => {
  const run = makeRun({
    plan: makeExecutionSnapshotPlan({
      stages: {
        pm: {
          status: "completed",
          completed_at: "2026-03-27T16:55:00Z",
          summary: "PM plan captured.",
        },
      },
    }),
  });
  let rerunPayload: unknown = null;

  await seedAdminSession(page);
  await mockRunDetailApis(page, {
    run,
    onCreateAttempt: (payload) => {
      rerunPayload = payload;
    },
  });

  await page.goto(`/runs/${run.run_id}`);

  await expect(page.getByText("Loading run details...")).toHaveCount(0, { timeout: 15000 });
  await page.getByTestId("run-rerun-trigger").click();
  await expect(page.getByTestId("rerun-option-fresh")).toBeVisible();
  await page.getByTestId("rerun-option-fresh").click();

  expect(rerunPayload).toEqual({ mode: "fresh" });
  await expect(page).toHaveURL(/8e8957f2-79f8-4dc8-8deb-786b2c93828d$/, { timeout: 15000 });
});

test("renders a dynamic team run without the fixed PM DEV TEST REVIEW stage bar", async ({ page }) => {
  const run = makeRun({
    issue_key: "TP-500",
    issue_summary: "Launch campaign team run",
    entry_stage: "team",
    team_run: {
      team_key: "campaign_team",
      team_label: "Campaign Team",
      definition_version: 2,
      status: "queued",
      artifacts: [],
      approvals: [],
      edges: [
        { from_task_key: "brief", to_task_key: "research" },
        { from_task_key: "research", to_task_key: "message_map" },
        { from_task_key: "message_map", to_task_key: "copy_draft" },
        { from_task_key: "copy_draft", to_task_key: "launch_review" },
      ],
      nodes: [
        {
          task_key: "brief",
          label: "Brief",
          owner_role_key: "strategist",
          owner_persona_key: "launch_strategist",
          owner_agent_key: "launch_strategy_agent",
          status: "completed",
          dependency_keys: [],
          artifact_contract: { produces: ["launch_brief"] },
          approval_rule: {},
        },
        {
          task_key: "research",
          label: "Research",
          owner_role_key: "researcher",
          owner_persona_key: "audience_researcher",
          owner_agent_key: "audience_research_agent",
          status: "completed",
          dependency_keys: ["brief"],
          artifact_contract: { produces: ["research_notes"] },
          approval_rule: {},
        },
        {
          task_key: "message_map",
          label: "Message Map",
          owner_role_key: "writer",
          owner_persona_key: "campaign_writer",
          owner_agent_key: "campaign_writer_agent",
          status: "running",
          dependency_keys: ["research"],
          artifact_contract: { produces: ["message_map"] },
          approval_rule: {},
        },
        {
          task_key: "copy_draft",
          label: "Copy Draft",
          owner_role_key: "writer",
          owner_persona_key: "campaign_writer",
          owner_agent_key: "campaign_writer_agent",
          status: "pending",
          dependency_keys: ["message_map"],
          artifact_contract: { produces: ["copy_draft"] },
          approval_rule: {},
        },
        {
          task_key: "launch_review",
          label: "Launch Review",
          owner_role_key: "reviewer",
          owner_persona_key: "launch_reviewer",
          owner_agent_key: "launch_review_agent",
          status: "pending",
          dependency_keys: ["copy_draft"],
          artifact_contract: { produces: ["launch_ready"] },
          approval_rule: { type: "manual" },
        },
      ],
    },
  });

  await seedAdminSession(page);
  await mockRunDetailApis(page, { run });

  await page.goto(`/runs/${run.run_id}`);

  await expect(page.getByText("Loading run details...")).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByText("Campaign Team", { exact: true })).toBeVisible();
  await expect(page.getByTestId("team-run-node-brief")).toContainText("Brief");
  await expect(page.getByTestId("team-run-node-research")).toContainText("Research");
  await expect(page.getByTestId("team-run-node-message_map")).toContainText("Message Map");
  await expect(page.getByTestId("team-run-node-copy_draft")).toContainText("Copy Draft");
  await expect(page.getByTestId("team-run-node-launch_review")).toContainText("Launch Review");
  await expect(page.getByText("PM", { exact: true })).toHaveCount(0);
  await expect(page.getByText("DEV", { exact: true })).toHaveCount(0);
  await expect(page.getByText("TEST", { exact: true })).toHaveCount(0);
  await expect(page.getByText("REVIEW", { exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "Agents" }).click();
  await expect(page.getByText("launch_strategy_agent")).toBeVisible();
  await expect(page.getByText("campaign_writer_agent")).toHaveCount(2);
  await expect(page.getByText("launch_review_agent")).toBeVisible();
});
