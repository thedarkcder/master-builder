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

test("shows artifact PR metadata for selected apps waiting on merge", async ({ page }) => {
  const tenant = makeTenant({ tenant_id: "example" });
  const project = makeProject({ tenant_id: "example", project_id: "example-default" });
  const app = makeProjectAppRecord({
    app_id: "app-1",
    tenant_id: tenant.tenant_id,
    project_id: project.project_id,
    name: "Payments API",
    slug: "payments-api",
    status: "needs_pr_merge",
    source_path: "apps/payments",
  });
  const analysisRun = makeProjectAppAnalysisRun({
    analysis_run_id: "analysis-1",
    tenant_id: tenant.tenant_id,
    project_id: project.project_id,
    status: "completed",
    raw_result: {
      artifact_pr: {
        pr_url: "https://github.com/example/repo/pull/42",
        generated_file_count: 3,
        generated_files: ["apps/payments/README.md", "apps/payments/deployment.yaml", "apps/payments/Dockerfile"],
      },
    },
  });

  await seedAdminSession(page);
  await page.route("**/api/bff/api/**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    if (pathname === "/api/bff/api/app/auth/me") {
      return fulfillJson(route, makePlatformAdminPrincipal());
    }
    if (pathname === "/api/bff/api/admin/tenants/example") {
      return fulfillJson(route, tenant);
    }
    if (pathname === "/api/bff/api/admin/tenants/example/projects/example-default") {
      return fulfillJson(route, project);
    }
    if (pathname === "/api/bff/api/admin/tenants/example/projects/example-default/apps") {
      return fulfillJson(route, [app]);
    }
    if (pathname === "/api/bff/api/admin/tenants/example/projects/example-default/apps/analysis-runs") {
      return fulfillJson(route, [analysisRun]);
    }
    if (pathname === "/api/bff/api/admin/tenants/example/projects/example-default/apps/app-1/deployment-config") {
      return fulfillJson(route, makeProjectAppDeploymentConfig({ app_id: app.app_id }));
    }
    if (pathname === "/api/bff/api/admin/tenants/example/projects/example-default/apps/app-1/deployment-releases") {
      return fulfillJson(route, []);
    }
    return route.fallback();
  });

  await page.goto("/example/projects/example-default/deployments/app-1");

  await expect(page.getByText("Generated deployment files must be merged before this deployment can run.")).toBeVisible();
  await expect(page.getByText("Artifact PR metadata")).toBeVisible();
  await expect(page.getByRole("link", { name: "Open PR" })).toHaveAttribute(
    "href",
    "https://github.com/example/repo/pull/42",
  );
  await expect(page.getByText("3 generated files")).toBeVisible();
  await expect(page.getByText("apps/payments/README.md, apps/payments/deployment.yaml, apps/payments/Dockerfile")).toBeVisible();
});
