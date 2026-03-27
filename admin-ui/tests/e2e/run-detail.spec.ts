import { expect, test } from "@playwright/test";

import {
  makeRun,
  makeStageInvocationLogs,
  mockRunDetailApis,
  seedAdminSession,
} from "./support/admin-ui";

test("renders checkpoint-backed failed-after-dev runs with separate execution and integration branches", async ({ page }) => {
  const run = makeRun({
    plan: {
      stage_checkpoints: {
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
    },
  });
  const logs = makeStageInvocationLogs({
    stage: "dev",
    invocationId: "dev-invocation-1",
    command: "stage.dev",
    startedAt: "2026-03-27T17:00:00Z",
    finishedAt: "2026-03-27T17:02:57Z",
    codexSessionId: run.dev_session_id ?? undefined,
  });

  await seedAdminSession(page);
  await mockRunDetailApis(page, { run, logs });

  await page.goto(`/runs/${run.run_id}`);

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
  await expect(page.getByText("PR created and code pushed.")).toBeVisible();
});

test("marks a finished stage without a checkpoint as interrupted on terminal runs", async ({ page }) => {
  const run = makeRun({
    plan: {
      stage_checkpoints: {
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
    },
  });
  const logs = makeStageInvocationLogs({
    stage: "dev",
    invocationId: "dev-invocation-1",
    command: "stage.dev",
    startedAt: "2026-03-27T17:00:00Z",
    finishedAt: "2026-03-27T17:02:57Z",
    codexSessionId: run.dev_session_id ?? undefined,
  });

  await seedAdminSession(page);
  await mockRunDetailApis(page, { run, logs });

  await page.goto(`/runs/${run.run_id}`);

  await expect(page.getByTestId("run-stage-dev")).toHaveAttribute("data-stage-status", "interrupted");
  await expect(page.getByTestId("run-stage-dev-detail")).toHaveText("interrupted");
  await expect(page.getByTestId("run-stage-test")).toHaveAttribute("data-stage-status", "not_started");

  await page.getByRole("button", { name: "Agents" }).click();
  await expect(page.getByText("agent finished, checkpoint missing")).toBeVisible();
});

test("offers review rerun when review state exists and posts the review resume payload", async ({ page }) => {
  const run = makeRun({
    plan: {
      stage_checkpoints: {
        pm: {
          status: "completed",
          completed_at: "2026-03-27T16:55:00Z",
          summary: "PM plan captured.",
        },
        dev: {
          status: "completed",
          completed_at: "2026-03-27T17:02:57Z",
          summary: "Code pushed.",
        },
        review: {
          status: "completed",
          completed_at: "2026-03-27T17:07:00Z",
          summary: "Review found follow-up items.",
        },
      },
      execution_context: {
        integration_branch: "feature/GP-124",
        execution_branch: "run/gp-124/5de2cedf-b7ae-400c-a53c-3beecf078a51",
      },
      review_summary: ["Address latest review feedback."],
    },
  });
  let rerunPayload: unknown = null;

  await seedAdminSession(page);
  await mockRunDetailApis(page, {
    run,
    onRerun: (payload) => {
      rerunPayload = payload;
    },
  });

  await page.goto(`/runs/${run.run_id}`);

  await page.getByTestId("run-rerun-trigger").click();
  await expect(page.getByTestId("rerun-option-review")).toBeVisible();
  await page.getByTestId("rerun-option-review").click();

  expect(rerunPayload).toEqual({ mode: "resume", resume_stage: "review" });
  await expect(page).toHaveURL(/8e8957f2-79f8-4dc8-8deb-786b2c93828d$/);
});
