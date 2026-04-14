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

test.setTimeout(60000);

test("gates managed restore until an execution is selected and queues an async restore run", async ({ page }) => {
  const tenant = makeTenant({ tenant_id: "route25" });
  const project = makeProject({ tenant_id: "route25", project_id: "route25-default" });
  const app = makeProjectAppRecord({
    app_id: "app-1",
    tenant_id: tenant.tenant_id,
    project_id: project.project_id,
    name: "Payments API",
    slug: "payments-api",
    status: "ready",
    source_path: "apps/payments",
  });
  const restoreRuns: Array<Record<string, unknown>> = [];
  let restoreRequestBody: Record<string, unknown> | null = null;

  await seedAdminSession(page);
  await page.route("**/api/bff/api/**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    if (pathname === "/api/bff/api/app/auth/me") {
      return fulfillJson(route, makePlatformAdminPrincipal());
    }
    if (pathname === "/api/bff/api/admin/tenants/route25") {
      return fulfillJson(route, tenant);
    }
    if (pathname === "/api/bff/api/admin/tenants/route25/projects/route25-default") {
      return fulfillJson(route, project);
    }
    if (pathname === "/api/bff/api/admin/tenants/route25/projects/route25-default/apps") {
      return fulfillJson(route, [app]);
    }
    if (pathname === "/api/bff/api/admin/tenants/route25/projects/route25-default/apps/analysis-runs") {
      return fulfillJson(route, []);
    }
    if (pathname === "/api/bff/api/admin/tenants/route25/projects/route25-default/apps/app-1/deployment-config") {
      return fulfillJson(
        route,
        makeProjectAppDeploymentConfig({
          app_id: app.app_id,
          resources: [
            {
              key: "db-primary",
              kind: "postgres",
              name: "Primary database",
              config: { coolify_uuid: "db-uuid-1" },
            },
          ],
          backup_policies: [
            {
              key: "db-daily",
              resource_key: "db-primary",
              enabled: true,
              schedule: "0 2 * * *",
              retention_days: 7,
              config: { coolify_backup_uuid: "backup-uuid-1" },
            },
          ],
        }),
      );
    }
    if (pathname === "/api/bff/api/admin/tenants/route25/projects/route25-default/apps/app-1/deployment-releases") {
      return fulfillJson(route, []);
    }
    if (pathname === "/api/bff/api/admin/tenants/route25/projects/route25-default/apps/app-1/deployment-backups/executions") {
      return fulfillJson(route, {
        backup_key: "db-daily",
        resource_key: "db-primary",
        backup_uuid: "backup-uuid-1",
        database_uuid: "db-uuid-1",
        executions: [
          {
            execution_uuid: "execution-1",
            status: "completed",
            created_at: "2026-03-27T18:00:00Z",
            started_at: "2026-03-27T18:00:05Z",
            completed_at: "2026-03-27T18:00:20Z",
            artifact_path: "/var/lib/coolify/backups/execution-1.sql",
            file_name: "execution-1.sql",
            details: {
              file_size: 2048,
            },
          },
        ],
      });
    }
    if (pathname === "/api/bff/api/admin/tenants/route25/projects/route25-default/apps/app-1/deployment-backups/restore-runs") {
      return fulfillJson(route, restoreRuns);
    }
    if (pathname === "/api/bff/api/admin/tenants/route25/projects/route25-default/apps/app-1/deployment-backups/restore") {
      restoreRequestBody = route.request().postDataJSON() as Record<string, unknown>;
      const createdRun = {
        restore_run_id: "restore-run-1",
        tenant_id: tenant.tenant_id,
        project_id: project.project_id,
        app_id: app.app_id,
        backup_policy_key: "db-daily",
        resource_key: "db-primary",
        backup_uuid: "backup-uuid-1",
        execution_uuid: "execution-1",
        database_type: "postgres",
        database_uuid: "db-uuid-1",
        restore_mode: "replace",
        requested_by_user_id: null,
        confirmation_value: "payments-api",
        execution_payload: {
          execution_uuid: "execution-1",
          artifact_path: "/var/lib/coolify/backups/execution-1.sql",
        },
        status: "queued",
        last_error: null,
        created_at: "2026-03-27T18:01:00Z",
        started_at: null,
        completed_at: null,
        updated_at: "2026-03-27T18:01:00Z",
      };
      restoreRuns.unshift(createdRun);
      return fulfillJson(route, createdRun, 201);
    }
    return route.fallback();
  });

  await page.goto("/route25/projects/route25-default/apps", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Backups" }).click();

  const restoreButton = page.getByRole("button", { name: "Run restore" });
  await expect(page.getByText("Restore execution list")).toBeVisible();
  await expect(page.getByLabel("Restore execution")).toBeEnabled();
  await expect(restoreButton).toBeDisabled();

  await page.getByLabel("Restore execution").selectOption("execution-1");
  await expect(restoreButton).toBeDisabled();

  await page.getByLabel("Confirm restore by typing the app slug").fill("payments-api");
  await expect(restoreButton).toBeEnabled();

  await restoreButton.click();

  expect(restoreRequestBody).toEqual({
    backup_key: "db-daily",
    resource_key: "db-primary",
    execution_uuid: "execution-1",
    confirmation_value: "payments-api",
    backup_uuid: "backup-uuid-1",
  });
  await expect(page.getByText("Queued restore run restore-run-1.")).toBeVisible();
  await expect(page.getByRole("row").filter({ hasText: "restore-run-1" })).toBeVisible();
});
