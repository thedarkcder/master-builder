import { expect, test } from "@playwright/test";

import {
  fulfillJson,
  makePlatformAdminPrincipal,
  makeProject,
  makeProjectAppAnalysisRun,
  makeProjectAppDeploymentConfig,
  makeProjectAppRecord,
  makeTenant,
  seedAdminSession,
} from "./support/admin-ui";

test("shows guided app launch for projects with no apps and starts repo analysis from create app", async ({ page }) => {
  const tenant = makeTenant({ tenant_id: "bsktpay-2" });
  const project = makeProject({
    tenant_id: tenant.tenant_id,
    project_id: "bsktpay-2-default",
    name: "BsktPay",
  });
  const analysisRun = makeProjectAppAnalysisRun({
    tenant_id: tenant.tenant_id,
    project_id: project.project_id,
    status: "queued",
    analysis_run_id: "analysis-guided-1",
  });
  let analysisStarted = false;

  await seedAdminSession(page);
  await page.route("**/api/bff/api/**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    if (pathname === "/api/bff/api/app/auth/me") {
      return fulfillJson(route, makePlatformAdminPrincipal());
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2") {
      return fulfillJson(route, tenant);
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default") {
      return fulfillJson(route, project);
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps") {
      return fulfillJson(route, []);
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/analysis-runs") {
      return fulfillJson(route, analysisStarted ? [analysisRun] : []);
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/analyze") {
      analysisStarted = true;
      return fulfillJson(route, analysisRun, 201);
    }
    return route.fallback();
  });

  await page.goto("/bsktpay-2/projects/bsktpay-2-default/apps");

  await expect(page.getByRole("heading", { name: "Launch an app" })).toBeVisible();
  await expect(page.getByText("No app has been created for this project yet.")).toBeVisible();
  await expect(page.getByText("Discovered apps")).toBeHidden();

  await page.getByRole("button", { name: "Create app" }).click();

  expect(analysisStarted).toBe(true);
  await expect(page.getByText("Queued analysis run analysis-guided-1.")).toBeVisible();
});

test("guides app launch through recommended settings and release creation", async ({ page }) => {
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
  let savedConfig: Record<string, unknown> | null = null;
  let releaseRequest: Record<string, unknown> | null = null;

  await seedAdminSession(page);
  await page.route("**/api/bff/api/**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    if (pathname === "/api/bff/api/app/auth/me") {
      return fulfillJson(route, makePlatformAdminPrincipal());
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2") {
      return fulfillJson(route, tenant);
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default") {
      return fulfillJson(route, project);
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps") {
      return fulfillJson(route, [app]);
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/analysis-runs") {
      return fulfillJson(route, []);
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/app-1/deployment-config") {
      if (route.request().method() === "PUT") {
        savedConfig = route.request().postDataJSON() as Record<string, unknown>;
        return fulfillJson(route, makeProjectAppDeploymentConfig({ app_id: app.app_id, ...(savedConfig ?? {}) }));
      }
      return fulfillJson(route, makeProjectAppDeploymentConfig({ app_id: app.app_id, environment_name: "", ...(savedConfig ?? {}) }));
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/app-1/deployment-releases") {
      if (route.request().method() === "POST") {
        releaseRequest = route.request().postDataJSON() as Record<string, unknown>;
        return fulfillJson(
          route,
          {
            release_id: "release-1",
            tenant_id: tenant.tenant_id,
            project_id: project.project_id,
            app_id: app.app_id,
            git_ref: null,
            commit_sha: null,
            reason: "Guided launch",
            deployment_snapshot: {},
            status: "queued",
            last_error: null,
            requested_by_user_id: null,
            created_at: "2026-05-07T12:00:00Z",
            started_at: null,
            completed_at: null,
            updated_at: "2026-05-07T12:00:00Z",
          },
          201,
        );
      }
      return fulfillJson(route, []);
    }
    if (pathname === "/api/bff/api/admin/tenants/bsktpay-2/projects/bsktpay-2-default/apps/app-1/deployment-backups/restore-runs") {
      return fulfillJson(route, []);
    }
    return route.fallback();
  });

  await page.goto("/bsktpay-2/projects/bsktpay-2-default/apps");

  await expect(page.getByRole("heading", { name: "Launch BsktPay Web" })).toBeVisible();
  await page.getByRole("button", { name: "Use recommended settings" }).click();
  await page.getByRole("button", { name: "Save launch settings" }).click();

  expect(savedConfig).toMatchObject({
    enabled: true,
    environment_name: "production",
    source_strategy: "dockerfile",
  });

  await page.getByRole("button", { name: "Launch live app" }).click();

  expect(releaseRequest).toMatchObject({ reason: "Guided launch" });
  await expect(page.getByText("Queued release release-1.")).toBeVisible();
});
