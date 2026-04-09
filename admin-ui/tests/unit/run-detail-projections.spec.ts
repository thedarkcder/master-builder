import { expect, test } from "@playwright/test";

import { buildChatTimelineEntries, parseRunLogChatText } from "../../lib/run-detail-projections/chat";
import { parseWorkflowDiagnostics } from "../../lib/run-detail-projections/diagnostics";
import { buildInvocationSessionRows, buildRunTimeline } from "../../lib/run-detail-projections/timeline";

test("parseRunLogChatText extracts command execution failures", async () => {
  const parsed = parseRunLogChatText({
    run_id: "run-1",
    issue_key: "MK-1",
    project_id: "project-1",
    agent_id: "runtime",
    invocation_id: "inv-1",
    channel: null,
    command: "workflow.dev",
    working_dir: "/tmp",
    stage: "dev",
    attempt: 1,
    stream: "stdout",
    message: JSON.stringify({
      type: "item.completed",
      item: {
        type: "command_execution",
        status: "failed",
        command: "pytest -q",
        aggregated_output: "tests failed",
        exit_code: 1,
      },
    }),
    recorded_at: "2026-04-09T10:00:00Z",
  });

  expect(parsed).not.toBeNull();
  expect(parsed?.kind).toBe("error");
  expect(parsed?.text).toContain("Command failed");
});

test("parseWorkflowDiagnostics reads runtime state history", async () => {
  const diagnostics = parseWorkflowDiagnostics({
    run_id: "run-2",
    workflow_id: "wf-2",
    attempt_number: 1,
    parent_run_id: null,
    entry_mode: "fresh",
    entry_stage: "team",
    entry_checkpoint_id: null,
    tenant_id: "tenant-a",
    project_id: "project-a",
    issue_key: "MK-2",
    issue_summary: "Launch",
    issue_url: null,
    repo_url: null,
    branch: null,
    pr_url: null,
    status: "blocked",
    waiting_for_input: false,
    pending_input_request_id: null,
    last_error: "blocked reason",
    created_at: "2026-04-09T09:55:00Z",
    started_at: "2026-04-09T09:56:00Z",
    finished_at: null,
    plan: null,
    team_run: {
      team_key: "marketing_launch",
      team_label: "Marketing Launch",
      definition_version: 1,
      status: "blocked",
      nodes: [],
      edges: [],
      artifacts: [],
      approvals: [],
      runtime_state: {
        history: [{ stage: "copy_draft", attempt: "1", event: "copy blocked" }],
      },
    },
  });

  expect(diagnostics).not.toBeNull();
  expect(diagnostics?.stage).toBe("copy_draft");
  expect(diagnostics?.history).toHaveLength(1);
  expect(diagnostics?.message).toBe("blocked reason");
});

test("timeline projections produce queue and invocation segments", async () => {
  const logs = [
    {
      run_id: "run-3",
      issue_key: "MK-3",
      project_id: "project-a",
      agent_id: "system",
      invocation_id: "inv-3",
      channel: null,
      command: "workflow.dev",
      working_dir: "/tmp",
      stage: "telemetry",
      attempt: 1,
      stream: "system",
      message: JSON.stringify({ event_kind: "queue_wait", queue_wait_ms: 2000 }),
      recorded_at: "2026-04-09T10:00:00Z",
    },
    {
      run_id: "run-3",
      issue_key: "MK-3",
      project_id: "project-a",
      agent_id: "system",
      invocation_id: "inv-3",
      channel: null,
      command: "workflow.dev",
      working_dir: "/tmp",
      stage: "telemetry",
      attempt: 1,
      stream: "system",
      message: JSON.stringify({ event_kind: "stage_invocation_started" }),
      recorded_at: "2026-04-09T10:00:02Z",
    },
    {
      run_id: "run-3",
      issue_key: "MK-3",
      project_id: "project-a",
      agent_id: "system",
      invocation_id: "inv-3",
      channel: null,
      command: "workflow.dev",
      working_dir: "/tmp",
      stage: "telemetry",
      attempt: 1,
      stream: "system",
      message: JSON.stringify({ event_kind: "stage_invocation_finished", status: "completed", duration_ms: 1500 }),
      recorded_at: "2026-04-09T10:00:03.5Z",
    },
  ];

  const invocationRows = buildInvocationSessionRows(logs as never);
  expect(invocationRows).toHaveLength(1);
  expect(invocationRows[0].stage).toBe("dev");

  const timeline = buildRunTimeline({
    run: {
      run_id: "run-3",
      workflow_id: "wf-3",
      attempt_number: 1,
      parent_run_id: null,
      entry_mode: "fresh",
      entry_stage: "team",
      entry_checkpoint_id: null,
      tenant_id: "tenant-a",
      project_id: "project-a",
      issue_key: "MK-3",
      issue_summary: null,
      issue_url: null,
      repo_url: null,
      branch: null,
      pr_url: null,
      status: "running",
      waiting_for_input: false,
      pending_input_request_id: null,
      last_error: null,
      created_at: "2026-04-09T10:00:00Z",
      started_at: "2026-04-09T10:00:02Z",
      finished_at: null,
      plan: null,
      team_run: null,
    },
    logs: logs as never,
    invocationSessionRows: invocationRows,
    isActiveRun: true,
  });
  expect(timeline).not.toBeNull();
  expect(timeline?.segments.some((segment) => segment.stage === "queue_wait")).toBeTruthy();
  expect(timeline?.segments.some((segment) => segment.stage === "dev")).toBeTruthy();
});

test("chat projection includes stage updates and diagnostics", async () => {
  const entries = buildChatTimelineEntries({
    run: {
      run_id: "run-4",
      workflow_id: "wf-4",
      attempt_number: 1,
      parent_run_id: null,
      entry_mode: "fresh",
      entry_stage: "team",
      entry_checkpoint_id: null,
      tenant_id: "tenant-a",
      project_id: "project-a",
      issue_key: "MK-4",
      issue_summary: null,
      issue_url: null,
      repo_url: null,
      branch: null,
      pr_url: null,
      status: "running",
      waiting_for_input: false,
      pending_input_request_id: null,
      last_error: null,
      created_at: "2026-04-09T09:00:00Z",
      started_at: "2026-04-09T09:01:00Z",
      finished_at: null,
      plan: {
        events: {
          stage_updates: [
            {
              stage: "copy_draft",
              jira_message: "Draft updated",
            },
          ],
        },
      },
      team_run: null,
    },
    logs: [],
    workflowDiagnostics: {
      stage: "copy_draft",
      message: "ok",
      history: [{ stage: "copy_draft", attempt: "1", event: "draft complete" }],
    },
  });

  expect(entries.length).toBeGreaterThan(0);
  expect(entries.some((entry) => entry.speaker === "system")).toBeTruthy();
  expect(entries.some((entry) => entry.speaker === "diagnostics")).toBeTruthy();
});
