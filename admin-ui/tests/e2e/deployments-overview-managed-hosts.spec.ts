import { expect, test } from "@playwright/test";

import {
  fulfillJson,
  makePlatformAdminPrincipal,
  makeTenant,
  seedAdminSession,
} from "./support/admin-ui";
import type { DeploymentHostRecord, TenantDeploymentPlaneRecord } from "../../lib/api/deployments";

test.setTimeout(60000);

test("platform admin can create and assign a managed host from deployments overview", async ({ page }) => {
  const tenant = makeTenant({ tenant_id: "example-workspace" });
  const createdHost: DeploymentHostRecord = {
    host_id: "host-2",
    label: "Primary managed host",
    provider: "internal_coolify",
    infrastructure_provider: "hetzner",
    region: "eu-west",
    capabilities: ["restore_database", "postgres"],
    agent_version: null,
    metadata: {},
    state: "provisioning",
    registered_at: null,
    last_seen_at: null,
    created_at: "2026-04-14T10:00:00Z",
    updated_at: "2026-04-14T10:00:00Z",
  };
  let createHostBody: Record<string, unknown> | null = null;
  let updatePlaneBody: Record<string, unknown> | null = null;
  let hosts: DeploymentHostRecord[] = [
    {
      host_id: "host-1",
      label: "Existing host",
      provider: "internal_coolify",
      infrastructure_provider: "hetzner",
      region: "eu-central",
      capabilities: ["restore_database", "postgres"],
      agent_version: "1.0.0",
      metadata: { rack: "a1" },
      state: "active",
      registered_at: "2026-04-14T08:00:00Z",
      last_seen_at: "2026-04-14T09:30:00Z",
      created_at: "2026-04-14T08:00:00Z",
      updated_at: "2026-04-14T09:30:00Z",
    },
  ];
  let deploymentPlane: TenantDeploymentPlaneRecord = {
    provider: "internal_coolify",
    infrastructure_provider: "hetzner",
    region: "eu-west",
    base_domain: "apps.example.com",
    platform_subdomain: "builder",
    api_base_url: "https://coolify.internal",
    coolify_project_uuid: null,
    coolify_environment_name: null,
    coolify_server_uuid: null,
    coolify_destination_uuid: null,
    managed_host_id: null,
    secret_refs: {},
    state: "active",
    last_error: null,
  };

  await seedAdminSession(page);
  await page.route("**/api/bff/api/**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    if (pathname === "/api/bff/api/app/auth/me") {
      return fulfillJson(route, makePlatformAdminPrincipal());
    }
    if (pathname === "/api/bff/api/admin/tenants/example-workspace") {
      return fulfillJson(route, tenant);
    }
    if (pathname === "/api/bff/api/admin/tenants/example-workspace/deployments/overview") {
      return fulfillJson(route, {
        summary: {
          total_apps: 0,
          draft_count: 0,
          needs_pr_merge_count: 0,
          ready_count: 0,
          deploying_count: 0,
          live_count: 0,
          failed_count: 0,
        },
        latest_failures: [],
        apps: [],
        generated_at: "2026-04-14T10:00:00Z",
      });
    }
    if (pathname === "/api/bff/api/admin/deployment-hosts" && route.request().method() === "GET") {
      return fulfillJson(route, hosts);
    }
    if (pathname === "/api/bff/api/admin/deployment-hosts" && route.request().method() === "POST") {
      createHostBody = route.request().postDataJSON() as Record<string, unknown>;
      hosts = [createdHost, ...hosts];
      return fulfillJson(
        route,
        {
          host: createdHost,
          bootstrap_token: "bootstrap-token-1",
        },
        201,
      );
    }
    if (pathname === "/api/bff/api/admin/tenants/example-workspace/deployment-plane" && route.request().method() === "GET") {
      return fulfillJson(route, deploymentPlane);
    }
    if (pathname === "/api/bff/api/admin/tenants/example-workspace/deployment-plane" && route.request().method() === "PUT") {
      updatePlaneBody = route.request().postDataJSON() as Record<string, unknown>;
      deploymentPlane = {
        ...deploymentPlane,
        ...(updatePlaneBody as typeof deploymentPlane),
      };
      return fulfillJson(route, deploymentPlane);
    }
    return route.fallback();
  });

  await page.goto("/example-workspace/deployments", { waitUntil: "domcontentloaded" });

  await expect(page.getByRole("heading", { name: "Managed hosts" })).toBeVisible();

  await page.getByLabel("Host label").fill("Primary managed host");
  await page.getByLabel("Region").fill("eu-west");
  await page.getByLabel("Capabilities").fill("restore_database\npostgres");
  await page.getByRole("button", { name: "Create managed host" }).click();

  expect(createHostBody).toEqual({
    label: "Primary managed host",
    provider: "internal_coolify",
    infrastructure_provider: "hetzner",
    region: "eu-west",
    capabilities: ["restore_database", "postgres"],
  });
  await expect(page.getByText("Created managed host Primary managed host. Recovery token is ready for manual agent bootstrap.")).toBeVisible();
  await expect(page.getByText("bootstrap-token-1")).toBeVisible();

  await page.getByLabel("Assigned host").selectOption("host-2");
  await page.getByRole("button", { name: "Save host assignment" }).click();

  expect(updatePlaneBody).toMatchObject({
    provider: "internal_coolify",
    managed_host_id: "host-2",
    state: "active",
  });
  await expect(page.getByText("Managed host assigned to the tenant deployment plane.")).toBeVisible();
});
