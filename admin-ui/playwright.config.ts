import { defineConfig, devices } from "@playwright/test";

const PLAYWRIGHT_APP_PORT = process.env.PLAYWRIGHT_APP_PORT ?? "4101";
const PLAYWRIGHT_BASE_URL = process.env.PLAYWRIGHT_BASE_URL ?? `http://localhost:${PLAYWRIGHT_APP_PORT}`;
const PLAYWRIGHT_RUN_LIVE = process.env.PLAYWRIGHT_RUN_LIVE === "1";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: PLAYWRIGHT_BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  webServer: {
    command: `npx next dev -p ${PLAYWRIGHT_APP_PORT}`,
    url: `${PLAYWRIGHT_BASE_URL}/login`,
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
    env: {
      NEXT_TELEMETRY_DISABLED: "1",
      NEXT_DIST_DIR: ".next-playwright",
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
