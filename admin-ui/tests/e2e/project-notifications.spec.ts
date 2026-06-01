import { expect, test } from "@playwright/test";

import {
  fulfillJson,
  installBffApiMocks,
  makeProject,
  makeTenant,
  seedAdminSession,
} from "./support/admin-ui";

test("project Discord live voice settings use clear empty placeholders and save entered IDs", async ({ page }) => {
  const tenant = makeTenant({
    tenant_id: "route25",
    name: "Route 25",
  });
  const project = makeProject({
    project_id: "route25-default",
    tenant_id: "route25",
    name: "Route 25 Default",
    discord: {
      notify_events: ["pr_opened"],
      live_voice_enabled: false,
      live_voice_channel_id: null,
      live_voice_linked_text_channel_id: null,
      live_voice_room_links: {},
    },
  });
  let savedPayload: unknown = null;

  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects",
      handler: (route) => fulfillJson(route, [project]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects/route25-default",
      handler: (route) => fulfillJson(route, project),
    },
    {
      method: "PATCH",
      pathname: "/api/bff/api/admin/tenants/route25/projects/route25-default/discord",
      handler: async (route) => {
        savedPayload = await route.request().postDataJSON();
        return fulfillJson(route, {
          ...project,
          discord: (savedPayload as { discord: typeof project.discord }).discord,
        });
      },
    },
  ]);

  await page.goto("/route25/projects/route25-default/notifications");

  await expect(page.getByRole("heading", { name: "Notification Settings" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Route 25 Default", level: 1 })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Access Requests" })).toHaveCount(0);
  await page.getByLabel("Enable live voice rooms for this project").check();
  await expect(page.getByLabel("Voice channel ID")).toHaveValue("");
  await expect(page.getByLabel("Voice channel ID")).toHaveAttribute("placeholder", "Paste Discord voice channel ID");
  await expect(page.getByLabel("Linked text channel or thread ID")).toHaveValue("");
  await expect(page.getByLabel("Linked text channel or thread ID")).toHaveAttribute("placeholder", "Paste Discord text channel or thread ID");

  await page.getByRole("button", { name: "Save Discord settings" }).click();
  await expect(page.getByText("Enter both the voice channel ID and linked text channel/thread ID.")).toBeVisible();
  expect(savedPayload).toBeNull();

  await page.getByLabel("Voice channel ID").fill("123456789012345678");
  await page.getByLabel("Linked text channel or thread ID").fill("987654321098765432");
  await expect(page.getByText("Enter both the voice channel ID and linked text channel/thread ID.")).toHaveCount(0);
  await page.getByRole("button", { name: "Save Discord settings" }).click();

  expect(savedPayload).toEqual({
    discord: {
      notify_events: ["pr_opened"],
      live_voice_enabled: true,
      live_voice_channel_id: "123456789012345678",
      live_voice_linked_text_channel_id: "987654321098765432",
      live_voice_room_links: {
        "123456789012345678": "987654321098765432",
      },
    },
  });
  await expect(page.getByText("Discord settings saved", { exact: true })).toBeVisible();
});

test("project access requests live on their own page and can be approved", async ({ page }) => {
  const tenant = makeTenant({
    tenant_id: "route25",
    name: "Route 25",
  });
  const project = makeProject({
    project_id: "route25-default",
    tenant_id: "route25",
    name: "Route 25 Default",
  });
  let approvedUserId: string | null = null;

  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects",
      handler: (route) => fulfillJson(route, [project]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects/route25-default",
      handler: (route) => fulfillJson(route, project),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/route25/projects/route25-default/discord/allowlist-requests",
      handler: (route) =>
        fulfillJson(
          route,
          approvedUserId
            ? []
            : [
                {
                  project_id: "route25-default",
                  user_id: "discord-user-1",
                  requested_at: "2026-06-01T09:30:00Z",
                  channel_id: "voice-1",
                  reason: "Needs access to the project room.",
                  permissions: [],
                },
              ],
        ),
    },
    {
      method: "POST",
      pathname: "/api/bff/api/admin/tenants/route25/projects/route25-default/discord/allowlist-requests/discord-user-1/approve",
      handler: (route) => {
        approvedUserId = "discord-user-1";
        return fulfillJson(route, {
          ok: true,
          details: "discord-user-1 can now access the project room.",
          project_id: "route25-default",
          user_id: "discord-user-1",
          notified: true,
        });
      },
    },
  ]);

  await page.goto("/route25/projects/route25-default/access-requests");

  await expect(page.getByRole("heading", { name: "Access Requests" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Route 25 Default", level: 1 })).toHaveCount(0);
  await expect(page.getByText("discord-user-1")).toBeVisible();
  await expect(page.getByText("Needs access to the project room.")).toBeVisible();
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("No pending requests")).toBeVisible();
  expect(approvedUserId).toBe("discord-user-1");
});
