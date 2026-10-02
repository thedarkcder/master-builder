import { expect, test } from "@playwright/test";

import {
  fulfillJson,
  installBffApiMocks,
  makeProject,
  makeTenant,
  seedAdminSession,
} from "./support/admin-ui";

test("project policy page saves the QA demo recording switch override", async ({ page }) => {
  const tenant = makeTenant({
    tenant_id: "example-workspace",
    name: "Example Workspace",
  });
  const project = makeProject({
    project_id: "example-workspace-default",
    tenant_id: "example-workspace",
    name: "Example Workspace Default",
    policy_overrides: {},
    effective_policy: {
      ...tenant.policy,
      qa_demo_recording_enabled: false,
    },
  });
  let savedPayload: unknown = null;

  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/example-workspace",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/codex/models",
      handler: (route) =>
        fulfillJson(route, {
          default_model: "gpt-5.4",
          default_reasoning_effort: "medium",
          runtime_kind: "codex_cli",
          profile_name: "engineering_execution",
          models: [],
          reasoning_efforts: ["low", "medium", "high"],
        }),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/example-workspace/projects",
      handler: (route) => fulfillJson(route, [project]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/example-workspace/projects/example-workspace-default",
      handler: (route) => fulfillJson(route, project),
    },
    {
      method: "PATCH",
      pathname: "/api/bff/api/admin/tenants/example-workspace/projects/example-workspace-default/policy",
      handler: async (route) => {
        savedPayload = await route.request().postDataJSON();
        return fulfillJson(route, {
          ...project,
          policy_overrides: {
            qa_demo_recording_enabled: true,
          },
          effective_policy: {
            ...project.effective_policy,
            qa_demo_recording_enabled: true,
          },
        });
      },
    },
  ]);

  await page.goto("/example-workspace/projects/example-workspace-default/settings");

  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await page.getByRole("button", { name: "Automation" }).click();
  const qaDemoRecordingField = page.getByText("QA demo recording").locator("..");
  await expect(qaDemoRecordingField).toContainText("Effective: Disabled");

  await qaDemoRecordingField.getByRole("button", { name: "On" }).click();
  await page.getByRole("button", { name: "Save settings" }).click();

  expect(savedPayload).toEqual({
    policy_overrides: {
      qa_demo_recording_enabled: true,
    },
  });
  await expect(page.getByText("Project policy saved")).toBeVisible();
  await expect(qaDemoRecordingField).toContainText("Effective: Enabled");
});
