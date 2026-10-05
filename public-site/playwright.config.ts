import { defineConfig, devices } from "@playwright/test";

const port = process.env.PUBLIC_SITE_TEST_PORT ?? "4603";
if (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535) {
  throw new Error("PUBLIC_SITE_TEST_PORT must be an integer between 1 and 65535");
}
const origin = `http://127.0.0.1:${port}`;
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { open: "never" }]],
  use: { baseURL: origin, trace: "retain-on-failure", screenshot: "only-on-failure", video: "retain-on-failure" },
  webServer: {
    command: "node scripts/serve.mjs",
    url: `${origin}/`,
    reuseExistingServer: false,
    env: { PUBLIC_SITE_PORT: port },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
