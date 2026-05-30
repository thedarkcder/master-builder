import { expect, test } from "@playwright/test";

import {
  fulfillJson,
  installBffApiMocks,
  makeProject,
  makeTenant,
  seedAdminSession,
} from "./support/admin-ui";

test("project plugin approvals ask for one clear approval without exposing schema fields", async ({ page }) => {
  const tenant = makeTenant({
    tenant_id: "example",
    name: "Route 25",
  });
  const project = makeProject({
    project_id: "example-default",
    tenant_id: "example",
    name: "Route 25 Default",
    github_repository: "thedarkcder/girl-power",
    jira_project_key: "GP",
  });
  let requestStatus = "pending";
  let oldRequestStatus = "pending";
  let approvedPayloadSeen = false;

  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/example",
      handler: (route) => fulfillJson(route, tenant),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/example/projects",
      handler: (route) => fulfillJson(route, [project]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/example/projects/example-default",
      handler: (route) => fulfillJson(route, project),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/runs",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/example/projects/example-default/discord/allowlist-requests",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/example/projects/example-default/installs",
      handler: (route) =>
        fulfillJson(route, {
          installs: [],
        }),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants/example/projects/example-default/install-requests",
      handler: (route) =>
        fulfillJson(route, {
          requests: [
            {
              request_id: "request-hubspot-old",
              tenant_id: "example",
              project_id: "example-default",
              workflow_id: "workflow-old",
              run_id: "run-old",
              issue_key: "AP-247",
              kind: "integration",
              label: "hubspot",
              reason: "Older duplicate HubSpot setup request.",
              suggested_config: {},
              required_bindings: [],
              status: oldRequestStatus,
              request_kind: "unsupported_kind",
              created_at: "2026-05-26T14:00:00Z",
              updated_at: "2026-05-26T14:00:00Z",
            },
            {
              request_id: "request-hubspot",
              tenant_id: "example",
              project_id: "example-default",
              workflow_id: "workflow-1",
              run_id: "run-1",
              issue_key: "AP-248",
              kind: "integration",
              label: "ap248_hubspot_install",
              reason: "AP-248 needs HubSpot billing data before implementation can continue.",
              suggested_config: {
                operator_decision:
                  "Approve Master Builder to add HubSpot support for this project so AP-248 can continue?",
                source_project_changes: ["Add HubSpot billing support to the project."],
                master_builder_changes: ["Allow this project to use HubSpot during runs."],
                package_changes: ["Add HubSpot client packages if implementation requires them."],
                secrets_or_bindings_needed: ["HubSpot API credentials."],
              },
              required_bindings: ["HUBSPOT_ACCESS_TOKEN"],
              status: requestStatus,
              request_kind: "unsupported_kind",
              created_at: "2026-05-26T15:00:00Z",
              updated_at: "2026-05-26T15:00:00Z",
            },
          ],
        }),
    },
    {
      method: "PUT",
      pathname: "/api/bff/api/admin/tenants/example/projects/example-default/install-requests/request-hubspot",
      handler: async (route) => {
        const payload = JSON.parse(route.request().postData() ?? "{}") as { status?: string };
        approvedPayloadSeen = payload.status === "approved";
        requestStatus = payload.status ?? requestStatus;
        oldRequestStatus = payload.status ?? oldRequestStatus;
        await fulfillJson(route, {
          request_id: "request-hubspot",
          tenant_id: "example",
          project_id: "example-default",
          workflow_id: "workflow-1",
          run_id: "run-1",
          issue_key: "AP-248",
          kind: "integration",
          label: "hubspot",
          reason: "AP-248 needs HubSpot billing data before implementation can continue.",
          suggested_config: {},
          required_bindings: ["HUBSPOT_ACCESS_TOKEN"],
          status: requestStatus,
          request_kind: "unsupported_kind",
          created_at: "2026-05-26T15:00:00Z",
          updated_at: "2026-05-26T15:05:00Z",
        });
      },
    },
  ]);

  await page.goto("/example/projects/example-default/installs");

  await expect(page.getByRole("heading", { name: "Plugin Approvals" })).toBeVisible();
  await expect(page.getByText("HubSpot", { exact: true })).toHaveCount(1);
  await expect(page.getByText("Approve HubSpot plugin")).toHaveCount(0);
  await expect(page.getByText("Use HubSpot for customer, company, invoice, and subscription data.")).toBeVisible();
  await expect(page.getByText("AP-248")).toHaveCount(0);
  await expect(page.getByText("Kind")).toHaveCount(0);
  await expect(page.getByText("Allowed bindings")).toHaveCount(0);
  await expect(page.getByText("Config JSON")).toHaveCount(0);
  await expect(page.getByText("Developer Setup Tools")).toHaveCount(0);

  await page.getByRole("button", { name: "Approve HubSpot" }).click();

  await expect(page.getByRole("button", { name: "Approved" })).toBeVisible();
  expect(approvedPayloadSeen).toBe(true);
});
