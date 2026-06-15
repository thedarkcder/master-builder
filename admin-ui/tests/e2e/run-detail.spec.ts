import { expect, test } from "@playwright/test";

import {
  makeExecutionSnapshotPlan,
  makeProjectAppRecord,
  makeRun,
  makeRuntimeStageLogs,
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
  await expect(page.getByRole("button", { name: "Logs" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Diagnostics" })).toHaveCount(0);
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

  await expect
    .poll(
      async () => {
        await page.getByRole("button", { name: "Agents" }).click().catch(() => undefined);
        return page.getByText("PR created and code pushed.", { exact: true }).count();
      },
      { timeout: 15000 },
    )
    .toBeGreaterThan(0);
});

test("keeps one run event stream while active run logs arrive", async ({ page }) => {
  const run = makeRun({
    status: "running",
  });
  const streamLogs = makeStageInvocationLogs({
    runId: run.run_id,
    stage: "dev",
    invocationId: "dev-live-stream",
    command: "stage.dev",
    startedAt: "2026-03-27T17:00:00Z",
  });
  let streamRequests = 0;

  await seedAdminSession(page);
  await mockRunDetailApis(page, {
    run,
    logs: [],
    streamEvents: streamLogs,
    onRunEventStreamRequest: () => {
      streamRequests += 1;
    },
  });

  await page.goto(`/runs/${run.run_id}/agents`);

  await expect(page.getByText("Loading run details...")).toHaveCount(0, { timeout: 15000 });
  await expect.poll(() => streamRequests, { timeout: 15000 }).toBe(1);
  expect(streamRequests).toBe(1);
});

test("marks an active stage as running when stage output exists without invocation telemetry", async ({ page }) => {
  const run = makeRun({
    status: "running",
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
  const logs = makeRuntimeStageLogs({
    runId: run.run_id,
    stage: "dev",
    invocationId: "dev-runtime-output",
    recordedAt: "2026-03-27T17:00:00Z",
    count: 3,
  });

  await seedAdminSession(page);
  await mockRunDetailApis(page, { run, logs });

  await page.goto(`/runs/${run.run_id}`);

  await expect(page.getByText("Loading run details...")).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByTestId("run-stage-dev")).toHaveAttribute("data-stage-status", "running");
  await expect(page.getByTestId("run-stage-dev-detail")).not.toHaveText("not started");
});

test("renders completed checkpoint stages in the run timeline without telemetry", async ({ page }) => {
  const run = makeRun({
    status: "succeeded",
    started_at: "2026-03-27T16:50:00Z",
    finished_at: "2026-03-27T17:02:00Z",
    plan: makeExecutionSnapshotPlan({
      stages: {
        pm: {
          status: "completed",
          completed_at: "2026-03-27T16:53:00Z",
          summary: "PM plan captured.",
        },
        dev: {
          status: "completed",
          completed_at: "2026-03-27T16:58:00Z",
          summary: "Implementation completed.",
        },
        test: {
          status: "completed",
          completed_at: "2026-03-27T17:00:00Z",
          summary: "Tests completed.",
        },
        review: {
          status: "completed",
          completed_at: "2026-03-27T17:02:00Z",
          summary: "Review completed.",
        },
      },
    }),
  });

  await seedAdminSession(page);
  await mockRunDetailApis(page, { run, logs: [] });

  await page.goto(`/runs/${run.run_id}`);

  await expect(page.getByText("Loading run details...")).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByTestId("run-timeline-segment-pm")).toBeVisible();
  await expect(page.getByTestId("run-timeline-segment-dev")).toBeVisible();
  await expect(page.getByTestId("run-timeline-segment-test")).toBeVisible();
  await expect(page.getByTestId("run-timeline-segment-review")).toBeVisible();
  await expect(page.getByText("Stage runtime").locator("..")).toContainText("12m 0s");
});

test("renders QA demo evidence links when the QA checkpoint is present", async ({ page }) => {
  const run = makeRun({
    status: "succeeded",
    plan: makeExecutionSnapshotPlan({
      stages: {
        pm: {
          status: "completed",
          completed_at: "2026-03-27T16:53:00Z",
          summary: "PM plan captured.",
        },
        dev: {
          status: "completed",
          completed_at: "2026-03-27T16:58:00Z",
          summary: "Implementation completed.",
        },
        test: {
          status: "completed",
          completed_at: "2026-03-27T17:00:00Z",
          summary: "Tests completed.",
        },
        review: {
          status: "completed",
          completed_at: "2026-03-27T17:02:00Z",
          summary: "Review completed.",
        },
        qa: {
          status: "completed",
          completed_at: "2026-03-27T17:05:00Z",
          summary: "QA recorded 2 demos across 2 walkthroughs.",
          artifact: {
            summary: ["QA recorded 2 demos across 2 walkthroughs."],
            scenarios: [
              { name: "Happy path", objective: "Show the feature works" },
              { name: "Break path", objective: "Try invalid inputs" },
            ],
            recordings: [
              {
                name: "Happy path demo",
                artifact_url: "https://cdn.example/qa/happy.webm",
                object_key: "tenant-1/project-1/run-1/qa-demo-1.webm",
                capture_target: "browser",
                capture_reference: "https://preview.example",
                content_sha256: "1111111111111111111111111111111111111111111111111111111111111111",
                release_context_sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
              },
              {
                name: "iOS break path demo",
                artifact_url: "https://cdn.example/qa/break.mp4",
                object_key: "tenant-1/project-1/run-1/qa-demo-2.mp4",
                capture_target: "ios",
                capture_reference: "ios-simulator://configured",
                content_sha256: "2222222222222222222222222222222222222222222222222222222222222222",
                release_context_sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
              },
            ],
            outcome: "continue",
          },
        },
      },
    }),
  });

  await seedAdminSession(page);
  await mockRunDetailApis(page, { run, logs: [] });

  await page.goto(`/runs/${run.run_id}/agents`);

  await expect(page.getByText("Loading run details...")).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByTestId("run-stage-qa")).toHaveAttribute("data-stage-status", "completed");
  await expect(page.getByRole("link", { name: "Happy path demo" })).toHaveAttribute(
    "href",
    "https://cdn.example/qa/happy.webm",
  );
  await expect(page.getByTestId("qa-recording-metadata-0")).toHaveText(
    "target=browser; reference=https://preview.example; object_key=tenant-1/project-1/run-1/qa-demo-1.webm; sha256=1111111111111111111111111111111111111111111111111111111111111111; release_context_sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  );
  await expect(page.getByRole("link", { name: "iOS break path demo" })).toHaveAttribute(
    "href",
    "https://cdn.example/qa/break.mp4",
  );
  await expect(page.getByTestId("qa-recording-metadata-1")).toHaveText(
    "target=ios; reference=ios-simulator://configured; object_key=tenant-1/project-1/run-1/qa-demo-2.mp4; sha256=2222222222222222222222222222222222222222222222222222222222222222; release_context_sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  );
});

test("links to the current preview when a succeeded run already has one", async ({ page }) => {
  const run = makeRun({
    run_id: "faedabdf-8433-4a65-a8ea-ca956314fa19",
    tenant_id: "bsktpay-2",
    project_id: "bsktpay-2-default",
    issue_key: "AP-293",
    issue_summary: "Add Equifax production credit bureau provider contract",
    status: "succeeded",
    branch: "feature/AP-293",
    finished_at: "2026-05-29T18:43:07Z",
  });
  const app = makeProjectAppRecord({
    app_id: "app-1",
    tenant_id: run.tenant_id,
    project_id: run.project_id ?? "bsktpay-2-default",
    name: "align",
  });
  const previewRelease = {
    release_id: "preview-release-1",
    tenant_id: run.tenant_id,
    project_id: run.project_id ?? "bsktpay-2-default",
    app_id: app.app_id,
    provider: "internal_coolify",
    release_kind: "run_preview" as const,
    status: "live",
    environment_name: "production",
    source_strategy: "docker_compose",
    git_ref: "mb/deploy/align/feature-ap-293",
    commit_sha: "abcdef1234567890",
    release_name: "AP-293: Add Equifax production credit bureau provider contract",
    source_run_id: run.run_id,
    source_issue_key: "AP-293",
    source_issue_summary: "Add Equifax production credit bureau provider contract",
    source_issue_url: "https://bsktpay.atlassian.net/browse/AP-293",
    pr_number: null,
    requested_by_user_id: null,
    deployment_snapshot: {},
    provider_context: {},
    service_urls: [
      {
        service_key: "admin-website",
        service_name: "Admin Website",
        service_kind: "website" as const,
        url: "http://admin.preview.align.192-168-0-118.sslip.io:8088",
        url_kind: "generated" as const,
        status: "active" as const,
      },
    ],
    last_error: null,
    requested_at: "2026-05-29T18:45:00Z",
    started_at: "2026-05-29T18:45:00Z",
    completed_at: "2026-05-29T18:47:00Z",
    created_at: "2026-05-29T18:45:00Z",
    updated_at: "2026-05-29T18:47:00Z",
  };
  let previewPostCount = 0;

  await seedAdminSession(page);
  await mockRunDetailApis(page, {
    run,
    deploymentApps: [app],
    deploymentReleasesByAppId: { [app.app_id]: [previewRelease] },
    onCreatePreview: () => {
      previewPostCount += 1;
    },
  });

  await page.goto(`/runs/${run.run_id}`);

  await expect(page.getByText("Loading run details...")).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByTestId("run-title")).toHaveText("Add Equifax production credit bureau provider contract");
  await expect(page.getByTestId("run-id")).toContainText(`Run ID ${run.run_id}`);
  await expect(page.getByRole("button", { name: "Generate preview" })).toHaveCount(0);
  const previewLink = page.getByRole("link", { name: "Open preview" });
  await expect(previewLink).toHaveAttribute(
    "href",
    "/bsktpay-2/projects/bsktpay-2-default/deployments/app-1?release=preview-release-1",
  );
  expect(previewPostCount).toBe(0);
});

test("links to the preview release page even when service URL is not active", async ({ page }) => {
  const run = makeRun({
    run_id: "faedabdf-8433-4a65-a8ea-ca956314fa19",
    tenant_id: "bsktpay-2",
    project_id: "bsktpay-2-default",
    issue_key: "AP-293",
    issue_summary: "Add Equifax production credit bureau provider contract",
    status: "succeeded",
    branch: "feature/AP-293",
    finished_at: "2026-05-29T18:43:07Z",
  });
  const app = makeProjectAppRecord({
    app_id: "app-1",
    tenant_id: run.tenant_id,
    project_id: run.project_id ?? "bsktpay-2-default",
    name: "align",
  });
  const previewRelease = {
    release_id: "preview-release-1",
    tenant_id: run.tenant_id,
    project_id: run.project_id ?? "bsktpay-2-default",
    app_id: app.app_id,
    provider: "internal_coolify",
    release_kind: "run_preview" as const,
    status: "route_activating",
    environment_name: "production",
    source_strategy: "docker_compose",
    git_ref: "mb/deploy/align/feature-ap-293",
    commit_sha: "abcdef1234567890",
    release_name: "AP-293: Add Equifax production credit bureau provider contract",
    source_run_id: run.run_id,
    source_issue_key: "AP-293",
    source_issue_summary: "Add Equifax production credit bureau provider contract",
    source_issue_url: "https://bsktpay.atlassian.net/browse/AP-293",
    pr_number: null,
    requested_by_user_id: null,
    deployment_snapshot: {},
    provider_context: {},
    service_urls: [],
    last_error: null,
    requested_at: "2026-05-29T18:45:00Z",
    started_at: "2026-05-29T18:45:00Z",
    completed_at: null,
    created_at: "2026-05-29T18:45:00Z",
    updated_at: "2026-05-29T18:47:00Z",
  };

  await seedAdminSession(page);
  await mockRunDetailApis(page, {
    run,
    deploymentApps: [app],
    deploymentReleasesByAppId: { [app.app_id]: [previewRelease] },
  });

  await page.goto(`/runs/${run.run_id}`);

  await expect(page.getByText("Loading run details...")).toHaveCount(0, { timeout: 15000 });
  const previewLink = page.getByRole("link", { name: "Open preview" });
  await expect(previewLink).toHaveAttribute(
    "href",
    "/bsktpay-2/projects/bsktpay-2-default/deployments/app-1?release=preview-release-1",
  );
  await expect(page.getByRole("button", { name: "Generate preview" })).toHaveCount(0);
});

test("refreshes active run checkpoints when live events advance the run", async ({ page }) => {
  const initialRun = makeRun({
    status: "running",
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
  const updatedRun = {
    ...initialRun,
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
    }),
  };

  await seedAdminSession(page);
  await mockRunDetailApis(page, {
    run: initialRun,
    runResponses: [initialRun, updatedRun],
    streamEvents: [
      {
        event_type: "TASK_STARTED",
        run_id: initialRun.run_id,
        issue_key: initialRun.issue_key,
        project_id: initialRun.project_id,
        agent_id: "dev",
        recorded_at: "2026-03-27T17:02:57Z",
      },
    ],
  });

  await page.goto(`/runs/${initialRun.run_id}`);

  await expect(page.getByText("Loading run details...")).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByTestId("run-stage-dev")).toHaveAttribute("data-stage-status", "completed");
  await expect(page.getByTestId("run-stage-dev-detail")).toContainText("completed");
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
  const resumeNavigation = page.waitForURL(/8e8957f2-79f8-4dc8-8deb-786b2c93828d$/, {
    timeout: 30000,
    waitUntil: "domcontentloaded",
  });
  await page.getByTestId("rerun-option-execution").click();
  await resumeNavigation;

  await expect.poll(() => rerunPayload).toEqual({ mode: "resume", checkpoint_kind: "execution" });
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
  const freshNavigation = page.waitForURL(/8e8957f2-79f8-4dc8-8deb-786b2c93828d$/, {
    timeout: 30000,
    waitUntil: "domcontentloaded",
  });
  await page.getByTestId("rerun-option-fresh").click();
  await freshNavigation;

  await expect.poll(() => rerunPayload).toEqual({ mode: "fresh" });
});

test("force rerun cancels the active run and starts fresh with no checkpoint payload", async ({ page }) => {
  const run = makeRun({
    status: "running",
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
  let cancelCalled = false;

  await seedAdminSession(page);
  await mockRunDetailApis(page, {
    run,
    onCancelRun: () => {
      cancelCalled = true;
    },
    onCreateAttempt: (payload) => {
      rerunPayload = payload;
    },
  });

  await page.goto(`/runs/${run.run_id}`);

  await expect(page.getByText("Loading run details...")).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByRole("button", { name: "Force Rerun" })).toBeVisible();
  const forceNavigation = page.waitForURL(/8e8957f2-79f8-4dc8-8deb-786b2c93828d$/, {
    timeout: 30000,
    waitUntil: "domcontentloaded",
  });
  await page.getByRole("button", { name: "Force Rerun" }).click();
  await forceNavigation;

  expect(cancelCalled).toBe(true);
  await expect.poll(() => rerunPayload).toEqual({ mode: "fresh" });
});
