import { expect, test } from "@playwright/test";

test("logs in with the default super admin and manages agent runtime routing", async ({ page }) => {
  page.on("dialog", (dialog) => {
    void dialog.accept();
  });

  await page.goto("/agent-runtimes");

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Admin sign in" })).toBeVisible();
  await expect(page.getByLabel("Username")).toHaveValue("admin");

  await page.getByLabel("Password").fill("change-me");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/dashboard$/);

  await page.goto("/agent-runtimes");

  await expect(page.getByRole("heading", { name: "Agent Runtimes" })).toBeVisible();
  await expect(page.getByText("Manage platform-wide routing from agent roles and named agents to built-in runtime profiles.")).toBeVisible();
  await expect(page.getByTestId("agent-runtimes-status")).toHaveText("Loaded platform agent runtime routing.");
  await expect(page.getByTestId("agent-runtime-role-row-pm")).toContainText("pm_conversation_default");
  await expect(page.getByTestId("agent-runtime-name-row-workflow_review_default")).toContainText(
    "engineering_execution_deep",
  );

  await page.getByTestId("agent-runtime-role-select-pm").selectOption("pm_conversation_fast");
  await page.getByRole("button", { name: "Save" }).click();

  await expect(page.getByTestId("agent-runtimes-status")).toHaveText("Saved platform agent runtime routing.");
  await expect(page.getByTestId("agent-runtime-role-select-pm")).toHaveValue("pm_conversation_fast");

  await page.getByRole("button", { name: "Reset" }).click();

  await expect(page.getByTestId("agent-runtimes-status")).toHaveText(
    "Reset platform agent runtime routing to inherited defaults.",
  );
  await expect(page.getByTestId("agent-runtime-role-select-pm")).toHaveValue("");
  await expect(page.getByTestId("agent-runtime-role-row-pm")).toContainText("pm_conversation_default");
});
