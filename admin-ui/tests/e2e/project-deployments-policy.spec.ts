import { expect, test } from "@playwright/test";

import {
  fulfillJson,
  makePlatformAdminPrincipal,
  makeProject,
  makeProjectAppDeploymentConfig,
  makeProjectAppRecord,
  makeTenant,
  seedAdminSession,
} from "./support/admin-ui";

test("empty deployment list does not expose a branch deploy trigger", async ({ page }) => {
  const tenant = makeTenant({ tenant_id: "bsktpay-2" });
  const project = makeProject({
    tenant_id: tenant.tenant_id,
    project_id: "bsktpay-2-default",
    name: "BsktPay",
  });

  await seedAdminSession(page);
  await page.route("**/api/bff/api/**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname.replace(/\/$/, "");
    if (pathname === "/api/bff/api/app/auth/me") return fulfillJson(route, makePlatformAdminPrincipal());
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2") return fulfillJson(route, tenant);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default") return fulfillJson(route, project);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps") return fulfillJson(route, []);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/analysis-runs") {
      return fulfillJson(route, []);
    }
    return route.fallback();
  });

  await page.goto("/bsktpay-2/projects/bsktpay-2-default/deployments");

  await expect(page.getByRole("heading", { name: "No deployments yet" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open deployment settings" })).toHaveAttribute(
    "href",
    "/bsktpay-2/projects/bsktpay-2-default/deployment",
  );
  await expect(page.getByText("Deploy branch")).toBeHidden();
  await expect(page.getByText("Create app")).toBeHidden();
  await expect(page.getByText("Launch app")).toBeHidden();
});

test("project deployment setup starts durable setup workflow without exposing infrastructure internals", async ({ page }) => {
  const tenant = makeTenant({ tenant_id: "bsktpay-2" });
  const project = makeProject({
    tenant_id: tenant.tenant_id,
    project_id: "bsktpay-2-default",
    name: "BsktPay",
  });
  let savedPayload: Record<string, unknown> | null = null;

  await seedAdminSession(page);
  await page.route("**/api/bff/api/**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname.replace(/\/$/, "");
    if (pathname === "/api/bff/api/app/auth/me") return fulfillJson(route, makePlatformAdminPrincipal());
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2") return fulfillJson(route, tenant);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default") return fulfillJson(route, project);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/github/branches") {
      return fulfillJson(route, [
        { name: "develop", protected: false },
        { name: "main", protected: true },
      ]);
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/deployment-setup") {
      savedPayload = route.request().postDataJSON() as Record<string, unknown>;
      return fulfillJson(route, {
        workflow_id: "deploy-setup:setup-run-1",
        status: "started",
        policy: savedPayload,
      });
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/deployment-policy") {
      return fulfillJson(route, {
        enabled: false,
        production_branch: null,
        preview_prs_enabled: false,
        provider: "internal_coolify",
        deployment_host_id: null,
        generated_domain_policy: "production",
        branch_settings: {},
        resources: [],
      });
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps") return fulfillJson(route, []);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/analysis-runs") {
      return fulfillJson(route, [
        {
          run_id: "deploy-setup:setup-run-1",
          tenant_id: tenant.tenant_id,
          project_id: project.project_id,
          status: "running",
          planner_version: null,
          request_payload: { analysis_source: "deployment_setup" },
          result_payload: {},
          error: null,
          created_at: "2026-05-08T10:00:00Z",
          started_at: "2026-05-08T10:00:01Z",
          completed_at: null,
          updated_at: "2026-05-08T10:00:01Z",
        },
      ]);
    }
    return route.fallback();
  });

  await page.goto("/bsktpay-2/projects/bsktpay-2-default/deployment");

  await expect(page.getByRole("heading", { name: "Deployments", exact: true })).toBeVisible();
  await expect(page.getByLabel("Deployment setup steps")).toContainText("Enable deployments");
  await expect(page.getByText("Provider")).toBeHidden();
  await expect(page.getByText("Host")).toBeHidden();
  await expect(page.getByText("Project defaults")).toBeHidden();
  await expect(page.getByRole("button", { name: "Add resource" })).toBeHidden();
  await expect(page.getByRole("button", { name: "Add secret ref" })).toBeHidden();
  await expect(page.getByText("Preview PR releases")).toBeHidden();
  await expect(page.getByLabel("Production branch")).toBeHidden();
  await expect(page.getByLabel("Generated URLs")).toBeHidden();

  await page.getByRole("radio", { name: /Enabled/ }).check();
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.getByLabel("Production branch")).toBeVisible();
  await expect(page.getByLabel("Generated URLs")).toBeHidden();
  await page.getByLabel("Production branch").selectOption("main");
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.getByLabel("Generated URLs")).toBeVisible();
  await page.getByLabel("Generated URLs").selectOption("production_and_preview");
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.locator("section").getByRole("heading", { name: "Environment", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Add variable" }).click();
  await page.getByLabel("Environment variable ENV_1 name").fill("APP_MODE");
  await page.getByLabel("Environment variable APP_MODE value").fill("production");
  await page.getByRole("button", { name: "Add secret ref" }).click();
  await page.getByLabel("Secret ref SECRET_1 name").fill("DATABASE_URL");
  await page.getByLabel("Secret ref DATABASE_URL value").fill("tenant/bsktpay-2/DATABASE_URL");
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.locator("section").getByRole("heading", { name: "Finish", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Back" })).toBeEnabled();
  await Promise.all([
    page.waitForURL(/\/bsktpay-2\/projects\/bsktpay-2-default\/deployments$/, { timeout: 15000 }),
    page.getByRole("button", { name: "Finish setup" }).click(),
  ]);

  expect(savedPayload).toEqual({
    enabled: true,
    production_branch: "main",
    preview_prs_enabled: false,
    provider: "internal_coolify",
    deployment_host_id: null,
    generated_domain_policy: "production_and_preview",
    branch_settings: {
      main: {
        environment: {
          APP_MODE: "production",
        },
        secret_refs: {
          DATABASE_URL: "tenant/bsktpay-2/DATABASE_URL",
        },
      },
    },
    resources: [],
  });
  await expect(page.getByRole("heading", { name: "Deployment setup is running" })).toBeVisible();
  await expect(page.getByText("MB is analyzing the selected branch")).toBeVisible();
});

test("configured deployment policy renders as settings instead of setup wizard", async ({ page }) => {
  const tenant = makeTenant({ tenant_id: "bsktpay-2" });
  const project = makeProject({
    tenant_id: tenant.tenant_id,
    project_id: "bsktpay-2-default",
    name: "BsktPay",
  });
  let savedPayload: Record<string, unknown> | null = null;

  await seedAdminSession(page);
  await page.route("**/api/bff/api/**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname.replace(/\/$/, "");
    if (pathname === "/api/bff/api/app/auth/me") return fulfillJson(route, makePlatformAdminPrincipal());
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2") return fulfillJson(route, tenant);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default") return fulfillJson(route, project);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/github/branches") {
      return fulfillJson(route, [
        { name: "main", protected: true },
        { name: "develop", protected: false },
      ]);
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/deployment-policy") {
      if (route.request().method() === "PUT") {
        savedPayload = route.request().postDataJSON() as Record<string, unknown>;
        return fulfillJson(route, savedPayload);
      }
      return fulfillJson(route, {
        enabled: true,
        production_branch: "main",
        preview_prs_enabled: false,
        provider: "internal_coolify",
        deployment_host_id: null,
        generated_domain_policy: "production",
        branch_settings: {
          main: {
            environment: { APP_MODE: "production" },
            secret_refs: { DATABASE_URL: "tenant/bsktpay-2/DATABASE_URL" },
          },
        },
        resources: [],
      });
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/deployment-setup") {
      throw new Error("configured policy save must not call deployment setup");
    }
    return route.fallback();
  });

  await page.goto("/bsktpay-2/projects/bsktpay-2-default/deployment");

  await expect(page.getByRole("heading", { name: "Deployment policy" })).toBeVisible();
  await expect(page.getByLabel("Deployment setup steps")).toHaveCount(0);
  await expect(page.getByLabel("Production branch")).toHaveValue("main");
  await expect(page.getByLabel("Generated URLs")).toHaveValue("production");
  await expect(page.getByLabel("Environment variable APP_MODE value")).toHaveValue("production");
  await page.getByRole("radio", { name: "Disabled" }).check();
  await page.getByRole("button", { name: "Save policy" }).click();

  expect(savedPayload).toMatchObject({
    enabled: false,
    production_branch: "main",
    generated_domain_policy: "production",
  });
  await expect(page).toHaveURL(/\/bsktpay-2\/projects\/bsktpay-2-default\/deployment$/);
});

test("disabled deployment setup jumps to finish instead of showing skipped environment setup", async ({ page }) => {
  const tenant = makeTenant({ tenant_id: "bsktpay-2" });
  const project = makeProject({
    tenant_id: tenant.tenant_id,
    project_id: "bsktpay-2-default",
    name: "BsktPay",
  });

  await seedAdminSession(page);
  await page.route("**/api/bff/api/**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname.replace(/\/$/, "");
    if (pathname === "/api/bff/api/app/auth/me") return fulfillJson(route, makePlatformAdminPrincipal());
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2") return fulfillJson(route, tenant);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default") return fulfillJson(route, project);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/github/branches") {
      return fulfillJson(route, [{ name: "main", protected: true }]);
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/deployment-policy") {
      return fulfillJson(route, {
        enabled: false,
        production_branch: null,
        preview_prs_enabled: false,
        provider: "internal_coolify",
        deployment_host_id: null,
        generated_domain_policy: "production",
        branch_settings: {},
        resources: [],
      });
    }
    return route.fallback();
  });

  await page.goto("/bsktpay-2/projects/bsktpay-2-default/deployment");

  await expect(page.getByRole("radio", { name: /Disabled/ })).toBeChecked();
  await page.getByRole("button", { name: "Next", exact: true }).click();

  await expect(page.locator("section").getByRole("heading", { name: "Finish", exact: true })).toBeVisible();
  await expect(page.getByText("Environment settings are skipped while deployments are disabled.")).toBeHidden();
  await expect(page.getByText("Branch settings")).toBeVisible();
});

test("lists existing deployments and opens the deployment admin route from manage", async ({ page }) => {
  const tenant = makeTenant({ tenant_id: "bsktpay-2" });
  const project = makeProject({
    tenant_id: tenant.tenant_id,
    project_id: "bsktpay-2-default",
    name: "BsktPay",
  });
  const app = makeProjectAppRecord({
    app_id: "app-1",
    tenant_id: tenant.tenant_id,
    project_id: project.project_id,
    name: "docker",
    slug: "docker",
    status: "ready",
    source_path: "api/docker",
    latest_release_name: "main @ abcdef12",
    latest_release_status: "live",
    latest_release_git_ref: "main",
    latest_release_commit_sha: "abcdef1234567890",
  });
  const release = {
    release_id: "release-1",
    tenant_id: tenant.tenant_id,
    project_id: project.project_id,
    app_id: app.app_id,
    provider: "internal_coolify",
    status: "live",
    environment_name: "production",
    source_strategy: "docker_compose",
    git_ref: "main",
    commit_sha: "abcdef1234567890",
    requested_by_user_id: null,
    deployment_snapshot: {},
    provider_context: {},
    service_urls: [
      {
        service_key: "web",
        service_name: "web",
        service_kind: "website",
        url: "https://web.generated.example.com",
        url_kind: "generated",
        status: "active",
      },
      {
        service_key: "api",
        service_name: "api",
        service_kind: "api",
        url: "https://api.generated.example.com",
        url_kind: "generated",
        status: "active",
      },
    ],
    last_error: null,
    requested_at: "2026-05-07T12:00:00Z",
    created_at: "2026-05-07T12:00:00Z",
    started_at: "2026-05-07T12:00:00Z",
    completed_at: "2026-05-07T12:01:00Z",
    updated_at: "2026-05-07T12:01:00Z",
  };

  await seedAdminSession(page);
  await page.route("**/api/bff/api/**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname.replace(/\/$/, "");
    if (pathname === "/api/bff/api/app/auth/me") return fulfillJson(route, makePlatformAdminPrincipal());
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2") return fulfillJson(route, tenant);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default") return fulfillJson(route, project);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps") return fulfillJson(route, [app]);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/app-1") return fulfillJson(route, app);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/analysis-runs") return fulfillJson(route, []);
    if (pathname.endsWith("/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/app-1/deployment-config")) {
      return fulfillJson(route, makeProjectAppDeploymentConfig({
        app_id: app.app_id,
        resources: [
          {
            key: "activemq",
            kind: "activemq",
            name: "activemq",
            config: {
              compose_service: "activemq",
              image: "apache/activemq-classic:6.1.7",
              service_type: "activemq",
              source: "docker_compose",
            },
          },
        ],
      }));
    }
    if (pathname.endsWith("/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/app-1/deployment-releases")) {
      return fulfillJson(route, [release]);
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/app-1/deployment-backups/restore-runs") {
      return fulfillJson(route, []);
    }
    return route.fallback();
  });

  await page.goto("/bsktpay-2/projects/bsktpay-2-default/deployments");

  await expect(page.getByRole("link", { name: /main @ abcdef12 live Latest release main @ abcdef12 Manage/ })).toBeVisible();
  await expect(page.getByText("api/docker")).toHaveCount(0);
  await expect(page.getByText("docker", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Deploy branch")).toBeHidden();
  await Promise.all([
    page.waitForURL(/\/bsktpay-2\/projects\/bsktpay-2-default\/deployments\/app-1$/, { timeout: 15000 }),
    page.getByRole("link", { name: /Manage/ }).click(),
  ]);
  const appControls = page.getByLabel("Deployment technical controls");
  await expect(appControls.getByRole("button", { name: "Settings" })).toBeVisible();
  await expect(appControls.getByRole("button", { name: "Environment" })).toBeVisible();
  await expect(appControls.getByRole("button", { name: "Deployments" })).toBeVisible();
  await expect(appControls.getByRole("button", { name: "Danger" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Deployment details" })).toBeVisible();
  await expect(page.getByRole("link", { name: "https://web.generated.example.com" }).first()).toBeVisible();
  await expect(page.getByText("Deployment history")).toBeVisible();
  await appControls.getByRole("button", { name: "Settings" }).click();
  await expect(page.getByText("Allow MB to run this deployment")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Runtime" })).toBeVisible();
  await appControls.getByRole("button", { name: "Environment" }).click();
  await expect(page.getByRole("heading", { name: "Environment" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Add variable" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Add secret" })).toBeVisible();
  await appControls.getByRole("button", { name: "Resources" }).click();
  await expect(page.locator('input[value="activemq"]').first()).toBeVisible();
  await expect(page.locator('select:has(option:checked[value="postgres"])')).toHaveCount(0);
  await expect(page.locator('select:has(option:checked[value="service"])')).toBeVisible();
  await appControls.getByRole("button", { name: "Domains" }).click();
  await expect(page.getByText("Generated release URLs")).toBeVisible();
  await expect(page.getByRole("link", { name: "https://api.generated.example.com" })).toBeVisible();
  await expect(page.getByText("No custom domains configured.")).toBeVisible();
});

test("removes a deployment from the danger area after slug confirmation", async ({ page }) => {
  const tenant = makeTenant({ tenant_id: "bsktpay-2" });
  const project = makeProject({
    tenant_id: tenant.tenant_id,
    project_id: "bsktpay-2-default",
    name: "BsktPay",
  });
  const app = makeProjectAppRecord({
    app_id: "app-1",
    tenant_id: tenant.tenant_id,
    project_id: project.project_id,
    name: "BsktPay Web",
    slug: "bsktpay-web",
    status: "ready",
    source_path: "apps/web",
  });
  let appRemoved = false;

  await seedAdminSession(page);
  await page.route("**/api/bff/api/**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname.replace(/\/$/, "");
    if (pathname === "/api/bff/api/app/auth/me") return fulfillJson(route, makePlatformAdminPrincipal());
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2") return fulfillJson(route, tenant);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default") return fulfillJson(route, project);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps") return fulfillJson(route, appRemoved ? [] : [app]);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/analysis-runs") return fulfillJson(route, []);
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/app-1") {
      if (route.request().method() === "DELETE") {
        appRemoved = true;
        return route.fulfill({ status: 204, body: "" });
      }
      return fulfillJson(route, app);
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/app-1/deployment-config") {
      return fulfillJson(route, makeProjectAppDeploymentConfig({ app_id: app.app_id }));
    }
    if (pathname.endsWith("/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/app-1/deployment-releases")) {
      return fulfillJson(route, []);
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/app-1/deployment-backups/restore-runs") {
      return fulfillJson(route, []);
    }
    return route.fallback();
  });

  await page.goto("/bsktpay-2/projects/bsktpay-2-default/deployments/app-1");

  await page.getByRole("button", { name: "Danger" }).click();
  await expect(page.getByRole("button", { name: "Remove deployment" })).toBeDisabled();
  await page.getByLabel("Type deployment slug to confirm").fill("bsktpay-web");
  await page.getByRole("button", { name: "Remove deployment" }).click();

  await expect(page).toHaveURL(/\/bsktpay-2\/projects\/bsktpay-2-default\/deployments$/, { timeout: 15000 });
  expect(appRemoved).toBe(true);
});
