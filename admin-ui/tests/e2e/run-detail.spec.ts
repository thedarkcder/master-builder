import { expect, test } from "@playwright/test";

import {
  makeExecutionSnapshotPlan,
  makeRun,
  makeStageInvocationLogs,
  mockRunDetailApis,
  seedAdminSession,
} from "./support/admin-ui";

test("renders legacy non-team runs as unsupported in the dynamic team-run UI", async ({ page }) => {
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

  await expect(
    page.getByText("This run does not include a team definition snapshot and is no longer supported in the dynamic team-run UI."),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "Failure Reason" })).toBeVisible();
  await expect(page.getByText(/heartbeat timeout/i)).toBeVisible();

  await page.getByRole("button", { name: "Agents" }).click();
  await expect(page.getByText("Legacy non-team runs are no longer rendered in the Agents panel.")).toBeVisible({
    timeout: 15000,
  });
});

test("shows non-team runs as unsupported in agents and stage-strip panels", async ({ page }) => {
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
  await expect(
    page.getByText("This run does not include a team definition snapshot and is no longer supported in the dynamic team-run UI."),
  ).toBeVisible();

  await page.getByRole("button", { name: "Agents" }).click();
  await expect(page.getByText("Legacy non-team runs are no longer rendered in the Agents panel.")).toBeVisible({
    timeout: 15000,
  });
});

test("offers latest-checkpoint resume and posts the workflow attempt payload", async ({ page }) => {
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
  await expect(page.getByTestId("rerun-option-resume-execution")).toBeVisible();
  await page.getByTestId("rerun-option-resume-execution").click();

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
        { from_task_key: "research", to_task_key: "visuals" },
        { from_task_key: "message_map", to_task_key: "copy_draft" },
        { from_task_key: "copy_draft", to_task_key: "launch_review" },
        { from_task_key: "visuals", to_task_key: "launch_review" },
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
          task_key: "visuals",
          label: "Visuals",
          owner_role_key: "designer",
          owner_persona_key: "creative_designer",
          owner_agent_key: "creative_designer_agent",
          status: "pending",
          dependency_keys: ["research"],
          artifact_contract: { produces: ["creative_assets"] },
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
          dependency_keys: ["copy_draft", "visuals"],
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
  await expect(page.getByTestId("team-run-node-visuals")).toContainText("Visuals");
  await expect(page.getByTestId("team-run-node-copy_draft")).toContainText("Copy Draft");
  await expect(page.getByTestId("team-run-node-launch_review")).toContainText("Launch Review");
  await expect(page.getByTestId("team-run-node-dependencies-brief")).toContainText("Start task");
  await expect(page.getByTestId("team-run-node-dependencies-research")).toContainText("Depends on: brief");
  await expect(page.getByTestId("team-run-node-dependencies-message_map")).toContainText("Depends on: research");
  await expect(page.getByTestId("team-run-node-dependencies-visuals")).toContainText("Depends on: research");
  await expect(page.getByTestId("team-run-node-dependencies-launch_review")).toContainText("Depends on: copy_draft, visuals");
  await expect(page.getByText("PM", { exact: true })).toHaveCount(0);
  await expect(page.getByText("DEV", { exact: true })).toHaveCount(0);
  await expect(page.getByText("TEST", { exact: true })).toHaveCount(0);
  await expect(page.getByText("REVIEW", { exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "Agents" }).click();
  await expect(page.getByText("launch_strategy_agent")).toBeVisible();
  await expect(page.getByText("campaign_writer_agent")).toHaveCount(2);
  await expect(page.getByText("creative_designer_agent")).toBeVisible();
  await expect(page.getByText("launch_review_agent")).toBeVisible();
});

test("renders stage updates from execution snapshot events", async ({ page }) => {
  const run = makeRun({
    status: "running",
    last_error: null,
    finished_at: null,
    plan: makeExecutionSnapshotPlan({
      stage_updates: [
        {
          stage: "message_map",
          jira_message: "Draft message map completed.",
        },
      ],
    }),
  });

  await seedAdminSession(page);
  await mockRunDetailApis(page, { run, logs: [] });

  await page.goto(`/runs/${run.run_id}`);

  await expect(page.getByText("Loading run details...")).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByText("Stage update: message_map. Draft message map completed.")).toBeVisible();
});
