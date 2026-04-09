import { expect, test } from "@playwright/test";

import {
  fulfillJson,
  installBffApiMocks,
  makePlatformAdminPrincipal,
  seedAdminSession,
} from "./support/admin-ui";

test("renders the platform team catalog and publishes a selected template", async ({ page }) => {
  let publishedVersion = 2;
  let publishCalls = 0;

  const personas = [
    {
      persona_id: "persona-1",
      persona_key: "launch_strategist",
      label: "Launch Strategist",
      description: "Owns the launch brief.",
      default_display_name: "Strategist",
      default_voice_id: null,
      system_prompt_template: null,
      user_prompt_template: null,
      allowed_surfaces: ["team_run_execution"],
      is_active: true,
      version: 1,
      published_at: null,
      created_at: "2026-04-09T08:00:00Z",
      updated_at: "2026-04-09T08:00:00Z",
    },
  ];

  const agents = [
    {
      agent_id: "agent-1",
      agent_key: "launch_strategy_agent",
      label: "Launch Strategy Agent",
      description: "Drives launch briefs.",
      persona_key: "launch_strategist",
      runtime_role_key: "strategist",
      named_agent_key: "launch_strategy_agent",
      selector_key: "team.launch.strategist",
      default_profile_name: "pm_conversation_default",
      is_active: true,
      version: 1,
      published_at: null,
      created_at: "2026-04-09T08:00:00Z",
      updated_at: "2026-04-09T08:00:00Z",
      persona: personas[0],
    },
  ];

  const buildTemplate = () => ({
    template_id: "template-1",
    team_key: "campaign_team",
    team_label: "Campaign Team",
    description: "Launch coordination team.",
    is_active: true,
    definition_version: publishedVersion,
    published_at: publishCalls > 0 ? "2026-04-09T09:00:00Z" : null,
    created_at: "2026-04-09T08:00:00Z",
    updated_at: "2026-04-09T08:00:00Z",
    roles: [
      {
        role_id: "role-1",
        role_key: "strategist",
        label: "Strategist",
        description: "Owns planning.",
        position: 1,
        persona: personas[0],
        agent: agents[0],
      },
    ],
    tasks: [
      {
        task_id: "task-1",
        task_key: "brief",
        label: "Brief",
        owner_role_key: "strategist",
        position: 1,
        artifact_contract: { produces: ["launch_brief"] },
        approval_rule: {},
      },
      {
        task_id: "task-2",
        task_key: "research",
        label: "Research",
        owner_role_key: "strategist",
        position: 2,
        artifact_contract: { produces: ["research_notes"] },
        approval_rule: {},
      },
      {
        task_id: "task-3",
        task_key: "message_map",
        label: "Message Map",
        owner_role_key: "strategist",
        position: 3,
        artifact_contract: { produces: ["message_map"] },
        approval_rule: {},
      },
      {
        task_id: "task-4",
        task_key: "copy_draft",
        label: "Copy Draft",
        owner_role_key: "strategist",
        position: 4,
        artifact_contract: { produces: ["copy_draft"] },
        approval_rule: {},
      },
      {
        task_id: "task-5",
        task_key: "launch_review",
        label: "Launch Review",
        owner_role_key: "strategist",
        position: 5,
        artifact_contract: { produces: ["launch_ready"] },
        approval_rule: { type: "manual" },
      },
    ],
    edges: [
      { edge_id: "edge-1", from_task_key: "brief", to_task_key: "research" },
      { edge_id: "edge-2", from_task_key: "research", to_task_key: "message_map" },
      { edge_id: "edge-3", from_task_key: "message_map", to_task_key: "copy_draft" },
      { edge_id: "edge-4", from_task_key: "copy_draft", to_task_key: "launch_review" },
    ],
  });

  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, makePlatformAdminPrincipal()),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/platform-personas",
      handler: (route) => fulfillJson(route, personas),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/platform-agents",
      handler: (route) => fulfillJson(route, agents),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/platform-team-templates",
      handler: (route) => fulfillJson(route, [buildTemplate()]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/platform-runtime-bindings",
      handler: (route) =>
        fulfillJson(route, {
          available_roles: ["strategist", "reviewer"],
          available_named_agents: ["launch_strategy_agent"],
          available_selectors: ["team.launch.strategist"],
        }),
    },
    {
      method: "POST",
      pathname: "/api/bff/api/admin/platform-team-templates/template-1/publish",
      handler: (route) => {
        publishCalls += 1;
        publishedVersion = 2;
        return fulfillJson(route, buildTemplate());
      },
    },
  ]);

  await page.goto("/platform-teams");

  await expect(page.getByRole("heading", { name: "Platform team catalog" })).toBeVisible();
  await expect(page.getByText("Campaign Team", { exact: true })).toBeVisible();
  await expect(page.locator('input[value="Brief"]')).toHaveCount(1);
  await expect(page.locator('input[value="Launch Review"]')).toHaveCount(1);

  await page.getByRole("button", { name: "Runtime bindings" }).click();
  await expect(page.getByText("team.launch.strategist")).toBeVisible();
  await expect(page.getByText("launch_strategy_agent")).toBeVisible();

  await page.getByRole("button", { name: "Teams" }).click();
  await page.getByRole("button", { name: "Publish selected" }).click();
  await expect(page.getByText("Published v2")).toBeVisible();
  expect(publishCalls).toBe(1);
});
