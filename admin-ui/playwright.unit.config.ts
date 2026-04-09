import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/unit",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : [["list"]],
});
