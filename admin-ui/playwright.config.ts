import { defineConfig, devices } from "@playwright/test";
import { randomBytes } from "node:crypto";

// The local E2E server and its cookie-generating test sender share one ephemeral key.
if (!process.env.AUTH_SECRET) {
  process.env.AUTH_SECRET = randomBytes(32).toString("hex");
}

function defaultCiPlaywrightPort(): string {
  const runId = Number.parseInt(process.env.GITHUB_RUN_ID ?? "", 10);
  if (Number.isFinite(runId) && runId > 0) {
    return String(4101 + (runId % 1000));
  }
  return "4101";
}

if (!process.env.PLAYWRIGHT_APP_PORT) {
  process.env.PLAYWRIGHT_APP_PORT = process.env.CI ? defaultCiPlaywrightPort() : "4101";
}

if (!process.env.PLAYWRIGHT_BASE_URL) {
  process.env.PLAYWRIGHT_BASE_URL = `http://localhost:${process.env.PLAYWRIGHT_APP_PORT}`;
}

const PLAYWRIGHT_APP_PORT = process.env.PLAYWRIGHT_APP_PORT;
const PLAYWRIGHT_BASE_URL = process.env.PLAYWRIGHT_BASE_URL;
const PLAYWRIGHT_RUN_LIVE = process.env.PLAYWRIGHT_RUN_LIVE === "1";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: !process.env.CI,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: PLAYWRIGHT_BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  webServer: {
    command: `npx next start -p ${PLAYWRIGHT_APP_PORT}`,
    url: `${PLAYWRIGHT_BASE_URL}/login`,
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
    env: {
      AUTH_SECRET: process.env.AUTH_SECRET,
      ORCHESTRATOR_API_BASE_URL: process.env.PLAYWRIGHT_API_BASE_URL ?? "http://localhost:60001",
      AUTH_TRUST_HOST: "true",
      NEXT_TELEMETRY_DISABLED: "1",
    },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
      testIgnore: ["**/*-live.spec.ts"],
    },
    ...(PLAYWRIGHT_RUN_LIVE
      ? [
          {
            name: "chromium-live",
            use: { ...devices["Desktop Chrome"] },
            testMatch: ["**/*-live.spec.ts"],
          },
        ]
      : []),
  ],
});
