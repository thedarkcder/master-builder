import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://localhost:4100",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  webServer: [
    {
      command:
        "cd .. && rm -f /tmp/master-builder-agent-runtimes-e2e.sqlite && " +
        "ORCHESTRATOR_DATABASE_URL=sqlite:////tmp/master-builder-agent-runtimes-e2e.sqlite " +
        "ORCHESTRATOR_AUTO_MIGRATE_ON_STARTUP=false " +
        ".venv/bin/python -m orchestrator migrate && " +
        "ORCHESTRATOR_DATABASE_URL=sqlite:////tmp/master-builder-agent-runtimes-e2e.sqlite " +
        "ORCHESTRATOR_AUTO_MIGRATE_ON_STARTUP=false " +
        ".venv/bin/python -m uvicorn orchestrator.api.main:app --host 127.0.0.1 --port 4400",
      url: "http://127.0.0.1:4400/health",
      timeout: 120_000,
      reuseExistingServer: false,
    },
    {
      command:
        "NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:4400 npm run build && " +
        "NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:4400 npm run start",
      url: "http://localhost:4100/login",
      timeout: 180_000,
      reuseExistingServer: false,
      env: {
        NEXT_TELEMETRY_DISABLED: "1",
      },
    },
  ],
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
