import { expect, test } from "@playwright/test";

import {
  fulfillJson,
  installAdminApiMocks,
  makeProject,
  makeProjectAutomation,
  makeProjectAutomationExecution,
  makeTenant,
  seedAdminSession,
} from "./support/admin-ui";

test("project automations render default drafts, save edits, and show execution history", async ({ page }) => {
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
  let savedPayload: unknown = null;

  await seedAdminSession(page);
  await installAdminApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/admin/auth/me",
      handler: (route) => fulfillJson(route, { username: "admin" }),
    },
    {
      method: "GET",
      pathname: "/api/admin/tenants/route25",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/admin/tenants/route25/projects",
      handler: (route) => fulfillJson(route, [project]),
    },
    {
      method: "GET",
      pathname: "/api/admin/tenants/route25/projects/route25-default",
      handler: (route) => fulfillJson(route, project),
    },
    {
      method: "GET",
      pathname: "/api/admin/tenants/route25/projects/route25-default/automations",
      handler: (route) => fulfillJson(route, { automations: [] }),
    },
    {
      method: "GET",
      pathname: "/api/admin/tenants/route25/projects/route25-default/discord/allowlist-requests",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "PUT",
      pathname: "/api/admin/tenants/route25/projects/route25-default/automations",
      handler: async (route) => {
        savedPayload = await route.request().postDataJSON();
        await fulfillJson(
          route,
          {
            automations: [
              makeProjectAutomation({
                automation_id: "automation-standup",
                kind: "standup_voice_brief",
                enabled: true,
                timezone: "Europe/London",
                days_of_week: [0, 1, 2, 3, 4],
                local_time: "09:30",
                fallback_lookback_hours: 36,
                last_successful_window_end_at: "2026-03-28T09:00:00Z",
                next_run_at: "2026-03-29T09:30:00Z",
                executions: [
                  makeProjectAutomationExecution({
                    execution_id: "exec-standup",
                    automation_id: "automation-standup",
                    scheduled_for: "2026-03-28T09:00:00Z",
                    window_start_at: "2026-03-28T08:00:00Z",
                    window_end_at: "2026-03-28T09:00:00Z",
                    status: "succeeded",
                    dedupe_key: "dedupe-standup",
                    started_at: "2026-03-28T08:01:00Z",
                    completed_at: "2026-03-28T08:04:00Z",
                    discord_message_id: "111111111111111111",
                    last_error: null,
                  }),
                ],
              }),
              makeProjectAutomation({
                automation_id: "automation-retro",
                kind: "retro_voice_brief",
                enabled: false,
                timezone: "Europe/London",
                days_of_week: [0, 3, 4],
                local_time: "16:00",
                fallback_lookback_hours: 72,
                last_successful_window_end_at: "2026-03-28T10:00:00Z",
                next_run_at: "2026-03-29T16:00:00Z",
                executions: [
                  makeProjectAutomationExecution({
                    execution_id: "exec-retro",
                    automation_id: "automation-retro",
                    scheduled_for: "2026-03-28T10:00:00Z",
                    window_start_at: "2026-03-28T09:00:00Z",
                    window_end_at: "2026-03-28T10:00:00Z",
                    status: "failed",
                    dedupe_key: "dedupe-retro",
                    started_at: "2026-03-28T09:01:00Z",
                    completed_at: "2026-03-28T09:05:00Z",
                    discord_message_id: "222222222222222222",
                    last_error: "Discord delivery failed.",
                  }),
                ],
              }),
            ],
          },
        );
      },
    },
  ]);

  await page.goto("/route25/projects/route25-default/automations");

  const standupCard = page.getByTestId("project-automation-standup_voice_brief");
  const retroCard = page.getByTestId("project-automation-retro_voice_brief");

  await expect(standupCard.getByText("Standup voice brief")).toBeVisible();
  await expect(retroCard.getByText("Retro voice brief")).toBeVisible();
  await expect(standupCard.getByLabel("Fallback lookback hours")).toHaveValue("24");
  await expect(retroCard.getByLabel("Fallback lookback hours")).toHaveValue("168");

  await standupCard.getByLabel("Enabled").check();
  await standupCard.getByLabel("Timezone").selectOption("Europe/London");
  await standupCard.getByLabel("Local time").fill("09:30");
  await standupCard.getByLabel("Fallback lookback hours").fill("36");

  await retroCard.getByLabel("Timezone").selectOption("Europe/London");
  await retroCard.getByLabel("Local time").fill("16:00");
  await retroCard.getByLabel("Fallback lookback hours").fill("72");
  await retroCard.getByLabel("Fri").uncheck();
  await retroCard.getByLabel("Mon").check();
  await retroCard.getByLabel("Thu").check();

  await page.getByRole("button", { name: "Save automations" }).click();

  await expect(page.getByText("Saved 2 automation configurations.")).toBeVisible();
  expect(savedPayload).toEqual({
    automations: [
      {
        kind: "standup_voice_brief",
        enabled: true,
        timezone: "Europe/London",
        days_of_week: [0, 1, 2, 3, 4],
        local_time: "09:30",
        fallback_lookback_hours: 36,
      },
      {
        kind: "retro_voice_brief",
        enabled: false,
        timezone: "Europe/London",
        days_of_week: [0, 3],
        local_time: "16:00",
        fallback_lookback_hours: 72,
      },
    ],
  });

  const executionRows = page.locator('[data-testid^="project-automation-execution-"]');
  await expect(executionRows).toHaveCount(2);
  await expect(executionRows.nth(0)).toContainText("Retro voice brief");
  await expect(executionRows.nth(0)).toContainText("2026-03-28T10:00:00Z");
  await expect(executionRows.nth(0)).toContainText("Discord message ID: 222222222222222222");
  await expect(executionRows.nth(0)).toContainText("Last error: Discord delivery failed.");
  await expect(executionRows.nth(1)).toContainText("Standup voice brief");
  await expect(executionRows.nth(1)).toContainText("2026-03-28T09:00:00Z");
  await expect(executionRows.nth(1)).toContainText("Discord message ID: 111111111111111111");
});
