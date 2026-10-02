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

test("project routes use project-scoped nav with a project picker instead of in-page section tabs", async ({ page }) => {
  const tenant = makeTenant({ tenant_id: "example-workspace", name: "Example Workspace" });
  const exampleProject = makeProject({ tenant_id: tenant.tenant_id, project_id: "example-workspace-default", name: "Example Workspace" });
  const paymentsProject = makeProject({ tenant_id: tenant.tenant_id, project_id: "payments", name: "Payments" });

  await seedAdminSession(page);
  await page.route("**/api/bff/api/**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname.replace(/\/$/, "");
    if (pathname === "/api/bff/api/app/auth/me") return fulfillJson(route, makePlatformAdminPrincipal());
    if (pathname === "/api/bff/api/admin/tenants/example-workspace") return fulfillJson(route, tenant);
    if (pathname === "/api/bff/api/admin/tenants/example-workspace/project-navigation") {
      return fulfillJson(route, [exampleProject, paymentsProject]);
    }
    if (pathname === "/api/bff/api/admin/tenants/example-workspace/projects/example-workspace-default") return fulfillJson(route, exampleProject);
    if (pathname === "/api/bff/api/admin/tenants/example-workspace/projects/example-workspace-default/apps") return fulfillJson(route, []);
    if (pathname === "/api/bff/api/admin/tenants/example-workspace/projects/example-workspace-default/apps/analysis-runs") return fulfillJson(route, []);
    return route.fallback();
  });

  await page.goto("/example-workspace/projects/example-workspace-default/deployments");

  await expect(page.getByRole("link", { name: "Back to Example Workspace workspace" })).toHaveAttribute(
    "href",
    "/example-workspace/dashboard",
  );
  await expect(page.getByLabel("Project")).toHaveValue("example-workspace-default");
  await expect(page.getByRole("link", { name: "Board" })).toHaveAttribute("href", "/example-workspace/projects/example-workspace-default");
  await expect(page.getByRole("link", { name: "Knowledge" })).toHaveAttribute("href", "/example-workspace/projects/example-workspace-default/knowledge");
  await expect(page.getByRole("link", { name: "Architecture" })).toHaveAttribute("href", "/example-workspace/projects/example-workspace-default/architecture");
  await expect(page.locator("summary").filter({ hasText: "Deployments" })).toHaveCount(0);
  await expect(page.locator("summary").filter({ hasText: "Development" })).toBeVisible();
  await expect(page.locator("summary").filter({ hasText: "Delivery" })).toBeVisible();
  await expect(page.locator("summary")).toHaveText(["Work", "Project", "Development", "Delivery"]);
  await expect(page.getByRole("link", { name: "Releases" })).toHaveAttribute("href", "/example-workspace/projects/example-workspace-default/deployments");
  await expect(page.getByRole("link", { name: "Policy" })).toHaveAttribute("href", "/example-workspace/projects/example-workspace-default/deployment");
  await page.locator("summary").filter({ hasText: "Project" }).click();
  await page.locator("summary").filter({ hasText: "Development" }).click();
  await expect(page.getByRole("link", { name: "Webhooks" })).toHaveAttribute("href", "/example-workspace/projects/example-workspace-default/webhooks");
  await expect(page.getByRole("link", { name: "Installs" })).toHaveAttribute("href", "/example-workspace/projects/example-workspace-default/installs");
  await expect(page.getByLabel("Project sections")).toBeHidden();

  await page.getByLabel("Project").selectOption("payments");
  await expect(page).toHaveURL(/\/example-workspace\/projects\/payments\/deployments$/);

  await page.goto("/example-workspace/projects/example-workspace-default/architecture");
  await page.getByLabel("Project").selectOption("payments");
  await expect(page).toHaveURL(/\/example-workspace\/projects\/payments\/architecture$/);
});

test("deployment detail header exposes a deployment selector", async ({ page }) => {
  const tenant = makeTenant({ tenant_id: "example-workspace", name: "Example Workspace" });
  const project = makeProject({ tenant_id: tenant.tenant_id, project_id: "example-workspace-default", name: "Example Workspace" });
  const web = makeProjectAppRecord({
    app_id: "web",
    tenant_id: tenant.tenant_id,
    project_id: project.project_id,
    name: "docker",
    slug: "docker",
    source_path: ".",
    latest_release_name: "main @ abcdef12",
    latest_release_status: "live",
    latest_release_git_ref: "main",
    latest_release_commit_sha: "abcdef1234567890",
  });
  const api = makeProjectAppRecord({
    app_id: "api",
    tenant_id: tenant.tenant_id,
    project_id: project.project_id,
    name: "API",
    slug: "api",
    source_path: ".",
  });

  await seedAdminSession(page);
  await page.route("**/api/bff/api/**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname.replace(/\/$/, "");
    if (pathname === "/api/bff/api/app/auth/me") return fulfillJson(route, makePlatformAdminPrincipal());
    if (pathname === "/api/bff/api/admin/tenants/example-workspace") return fulfillJson(route, tenant);
    if (pathname === "/api/bff/api/admin/tenants/example-workspace/project-navigation") return fulfillJson(route, [project]);
    if (pathname === "/api/bff/api/admin/tenants/example-workspace/projects/example-workspace-default") return fulfillJson(route, project);
    if (pathname === "/api/bff/api/admin/tenants/example-workspace/projects/example-workspace-default/apps") return fulfillJson(route, [web, api]);
    if (pathname === "/api/bff/api/admin/tenants/example-workspace/projects/example-workspace-default/apps/analysis-runs") return fulfillJson(route, []);
    if (pathname === "/api/bff/api/admin/tenants/example-workspace/projects/example-workspace-default/apps/web") return fulfillJson(route, web);
    if (pathname === "/api/bff/api/admin/tenants/example-workspace/projects/example-workspace-default/apps/api") return fulfillJson(route, api);
    if (pathname.endsWith("/deployment-config")) {
      return fulfillJson(route, makeProjectAppDeploymentConfig({ app_id: pathname.includes("/apps/api/") ? api.app_id : web.app_id }));
    }
    if (pathname.endsWith("/deployment-releases")) {
      return fulfillJson(route, pathname.includes("/apps/web/")
        ? [
            {
              release_id: "release-web-1",
              tenant_id: tenant.tenant_id,
              project_id: project.project_id,
              app_id: web.app_id,
              provider: "internal_coolify",
              status: "live",
              environment_name: "production",
              source_strategy: "docker_compose",
              git_ref: "main",
              commit_sha: "abcdef1234567890",
              release_name: "main @ abcdef12",
              requested_by_user_id: null,
              deployment_snapshot: {},
              provider_context: {},
              service_urls: [
                {
                  service_key: "web",
                  service_name: "dejavu",
                  service_kind: "website",
                  url: "https://web.example.test",
                  url_kind: "generated",
                  status: "active",
                },
              ],
              last_error: null,
              requested_at: "2026-05-08T10:00:00Z",
              created_at: "2026-05-08T10:00:00Z",
              started_at: "2026-05-08T10:00:00Z",
              completed_at: "2026-05-08T10:01:00Z",
              updated_at: "2026-05-08T10:01:00Z",
            },
          ]
        : []);
    }
    if (pathname.endsWith("/deployment-backups/restore-runs")) return fulfillJson(route, []);
    return route.fallback();
  });

  await page.route("https://web.example.test/**", (route) => route.fulfill({
    status: 200,
    contentType: "text/html",
    body: "<!doctype html><title>Example deployment preview</title>",
  }));

  await page.goto("/example-workspace/projects/example-workspace-default/deployments/web");

  await expect(page.getByLabel("Deployment selector")).toHaveValue("app:web");
  await expect(page.locator("#deployment-selector option:checked")).toHaveText("Main: main @ abcdef12");
  await expect(page.getByRole("heading", { name: "main @ abcdef12" })).toBeVisible();
  await expect(page.locator("iframe[title='main @ abcdef12 preview']")).toHaveAttribute("src", "https://web.example.test");
  await expect(page.locator("iframe[title='main @ abcdef12 preview']")).toHaveCSS("width", "1440px");
  await expect(page.getByText("api/docker")).toHaveCount(0);
  await expect(page.getByText("docker", { exact: true })).toHaveCount(0);
  await page.getByLabel("Deployment selector").selectOption("app:api");
  await expect(page).toHaveURL(/\/example-workspace\/projects\/example-workspace-default\/deployments\/api$/);
});
