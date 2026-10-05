import { expect, test } from "@playwright/test";

const repositoryUrl = "https://github.com/thedarkcder/master-builder";
const githubApiUrl = "https://api.github.com/repos/thedarkcder/master-builder";
const publicRepository = { full_name: "thedarkcder/master-builder", private: false, stargazers_count: 137 };

test("Get Started opens the actual source repository", async ({ page }) => {
  await page.route(githubApiUrl, (route) => route.fulfill({ status: 404, json: { message: "Not Found" } }));
  await page.route(repositoryUrl, (route) => route.fulfill({ contentType: "text/html", body: "<h1>Repository destination</h1>" }));
  await page.goto("/master-builder/");
  const start = page.getByRole("link", { name: "Get Started", exact: true }).first();
  await expect(start).toHaveAttribute("href", repositoryUrl);
  await start.click();
  await expect(page).toHaveURL(repositoryUrl);
  await expect(page.getByRole("heading", { name: "Repository destination" })).toBeVisible();
});

test("GitHub stars show the verified external count and remain a repository link", async ({ page }) => {
  await page.route(githubApiUrl, async (route) => {
    expect(route.request().headers().authorization).toBeUndefined();
    expect(route.request().headers().cookie).toBeUndefined();
    await route.fulfill({ json: publicRepository });
  });
  await page.goto("/master-builder/");
  const stars = page.getByRole("link", { name: "Star on GitHub" });
  await expect(stars).toHaveAttribute("href", repositoryUrl);
  await expect(stars).toContainText("137 stars");
  await expect(page.getByText("Stars unavailable", { exact: true })).toHaveCount(0);
});

test("a verified zero star count is shown as zero", async ({ page }) => {
  await page.route(githubApiUrl, (route) => route.fulfill({ json: { ...publicRepository, stargazers_count: 0 } }));
  await page.goto("/master-builder/");
  await expect(page.getByRole("link", { name: "Star on GitHub" })).toContainText("0 stars");
});

for (const status of [404, 429, 503]) {
  test(`GitHub HTTP ${status} explicitly reports stars unavailable`, async ({ page }) => {
    await page.route(githubApiUrl, (route) => route.fulfill({ status, json: { message: "Unavailable" } }));
    await page.goto("/master-builder/");
    await expect(page.getByRole("link", { name: "Star on GitHub" })).toContainText("Stars unavailable");
    await expect(page.getByRole("link", { name: "Get Started", exact: true }).first()).toBeVisible();
  });
}

for (const [name, response] of [
  ["private repository", { ...publicRepository, private: true }],
  ["wrong repository", { ...publicRepository, full_name: "example/another-project" }],
  ["invalid star count", { ...publicRepository, stargazers_count: "9000" }],
  ["negative star count", { ...publicRepository, stargazers_count: -1 }],
  ["fractional star count", { ...publicRepository, stargazers_count: 1.5 }],
  ["unsafe integer star count", { ...publicRepository, stargazers_count: Number.MAX_SAFE_INTEGER + 1 }],
] as const) {
  test(`${name} cannot supply a public star count`, async ({ page }) => {
    await page.route(githubApiUrl, (route) => route.fulfill({ json: response }));
    await page.goto("/master-builder/");
    const stars = page.getByRole("link", { name: "Star on GitHub" });
    await expect(stars).toContainText("Stars unavailable");
    await expect(stars).not.toContainText("137 stars");
    await expect(stars).not.toContainText("9000");
  });
}

test("offline GitHub explicitly reports stars unavailable", async ({ page }) => {
  await page.route(githubApiUrl, (route) => route.abort("internetdisconnected"));
  await page.goto("/master-builder/");
  await expect(page.getByRole("link", { name: "Star on GitHub" })).toContainText("Stars unavailable");
});

test("pending GitHub evidence shows loading until the external response arrives", async ({ page }) => {
  let release!: () => void;
  const responseReady = new Promise<void>((resolve) => { release = resolve; });
  await page.route(githubApiUrl, async (route) => {
    await responseReady;
    await route.fulfill({ json: publicRepository });
  });
  await page.goto("/master-builder/", { waitUntil: "domcontentloaded" });
  const stars = page.getByRole("link", { name: "Star on GitHub" });
  await expect(stars).toContainText("Loading stars");
  release();
  await expect(stars).toContainText("137 stars");
});

for (const width of [375, 1440]) {
  test(`homepage is readable and keyboard navigable at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.route(githubApiUrl, (route) => route.fulfill({ status: 404, json: { message: "Not Found" } }));
    await page.goto("/master-builder/");
    await expect(page.getByRole("heading", { level: 1, name: "Open-source software factory" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "From work item to reviewed change" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Source setup" })).toBeVisible();
    await expect(page.getByRole("link", { name: "AGPL-3.0-only", exact: true }).first()).toBeVisible();
    await expect(page.getByText(/Mobile QA, voice and provider deployment integrations are experimental/)).toBeVisible();
    await expect(page.locator('a[href^="/blog"]')).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Sign in", exact: true })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.keyboard.press("Tab");
    await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("main")).toBeFocused();
    // Capture the resting page after the keyboard-focus assertion above.
    await page.getByRole("heading", { level: 1 }).click();
    await page.screenshot({ path: testInfo.outputPath(`homepage-${width}-viewport.png`) });
    await page.screenshot({ path: testInfo.outputPath(`homepage-${width}.png`), fullPage: true });
  });
}

test("a GitHub response that never arrives reaches an explicit unavailable state", async ({ page }) => {
  await page.route(githubApiUrl, () => { /* The external boundary intentionally never responds. */ });
  await page.goto("/master-builder/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("link", { name: "Star on GitHub" })).toContainText("Stars unavailable", { timeout: 10_000 });
});

test("homepage privacy navigation explains the anonymous statistics request", async ({ page }) => {
  await page.route(githubApiUrl, (route) => route.fulfill({ status: 404, json: { message: "Not Found" } }));
  await page.goto("/master-builder/");
  await page.getByRole("link", { name: "Privacy", exact: true }).click();
  await expect(page).toHaveURL(/\/master-builder\/privacy\/$/);
  await expect(page.getByRole("heading", { name: "Public homepage statistics" })).toBeVisible();
  await expect(page.getByText(/No credentials or cookies are sent with this request/)).toBeVisible();
  await expect(page.getByText(/GitHub receives normal network metadata, such as your IP address/)).toBeVisible();
  await expect(page.getByRole("link", { name: "GitHub privacy statement" })).toHaveAttribute("href", "https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement");
});

test("malformed GitHub JSON cannot supply a public star count", async ({ page }) => {
  await page.route(githubApiUrl, (route) => route.fulfill({ contentType: "application/json", body: "{invalid" }));
  await page.goto("/master-builder/");
  await expect(page.getByRole("link", { name: "Star on GitHub" })).toContainText("Stars unavailable");
});

test("leaving the homepage aborts its pending GitHub request", async ({ page }) => {
  await page.route(githubApiUrl, () => { /* Leave the external response pending to observe cancellation. */ });
  const requested = page.waitForRequest(githubApiUrl);
  await page.goto("/master-builder/", { waitUntil: "domcontentloaded" });
  await requested;
  const cancelled = page.waitForEvent("requestfailed", { predicate: (request) => request.url() === githubApiUrl });
  await page.getByRole("link", { name: "Privacy", exact: true }).click();
  await expect(page).toHaveURL(/\/master-builder\/privacy\/$/);
  expect((await cancelled).failure()?.errorText).toContain("ERR_ABORTED");
  await expect(page.getByRole("link", { name: "Star on GitHub" })).toHaveCount(0);
});

test("the homepage font notice opens the actual served license", async ({ page }) => {
  const available = await page.request.get("/master-builder/licenses/manrope-OFL.txt", { maxRedirects: 0 });
  expect(available.status()).toBe(200);
  const copyright = await page.request.get("/master-builder/licenses/manrope-NOTICE.txt", { maxRedirects: 0 });
  expect(copyright.status()).toBe(200);
  expect(await copyright.text()).toContain("Copyright 2019 The Manrope Project Authors");
  await page.route(githubApiUrl, (route) => route.fulfill({ status: 404, json: { message: "Not Found" } }));
  await page.goto("/master-builder/");
  const notice = page.getByRole("link", { name: "Font license", exact: true });
  await expect(notice).toHaveAttribute("href", "/master-builder/licenses/manrope-OFL.txt");
  const response = page.waitForResponse((response) => response.url().endsWith("/master-builder/licenses/manrope-OFL.txt"));
  await notice.click();
  expect((await response).status()).toBe(200);
  await expect(page).toHaveURL(/\/licenses\/manrope-OFL\.txt$/);
  await expect(page.locator("body")).toContainText("Copyright 2018 The Manrope Project Authors");
  await expect(page.locator("body")).toContainText("SIL OPEN FONT LICENSE Version 1.1");
  await expect(page.locator("body")).toContainText("Permission is hereby granted, free of charge");
});

test("unlisted license paths return a real static 404", async ({ page }) => {
  const response = await page.request.get("/master-builder/licenses/unlisted-file.txt", { maxRedirects: 0 });
  expect(response.status()).toBe(404);
  expect(response.headers().location).toBeUndefined();
});


test("homepage documents user capabilities and delivery boundaries without a blog", async ({ page }) => {
  await page.route(githubApiUrl, (route) => route.fulfill({ status: 404, json: { message: "Not Found" } }));
  await page.goto("/master-builder/");
  await expect(page.getByRole("heading", { level: 1, name: "Open-source software factory" })).toBeVisible();
  await expect(page).toHaveTitle("Open Factory — Open-source software factory");
  await expect(page.locator('meta[name="description"]')).toHaveAttribute("content", "Open Factory is an extensible open-source software factory. Encode your engineering practices in configurable workflows, agent profiles, policies and review gates; plan, build, test and review software in your own environment.");
  await expect(page.getByRole("main")).not.toContainText(/AI-assisted/i);
  for (const heading of ["Work planning and human decisions", "Workflow execution", "Run inspection and token usage", "Projects and team access", "Project knowledge", "Code review in GitHub", "Deployment previews and QA", "Discord collaboration", "Scheduled team briefings", "From work item to reviewed change", "Source setup", "Status and boundaries", "License"]) {
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
  }
  await expect(page.locator('a[href^="/blog"]')).toHaveCount(0);
  await expect(page.getByText("Bring the moving parts of delivery together.", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Your infrastructure. Your source code.", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Planning and decision rules", exact: true })).toHaveAttribute("href", `${repositoryUrl}/blob/main/docs/run-decision-engine.md#decision-gate-contract`);
  await expect(page.getByRole("link", { name: "Runtime requirements", exact: true })).toHaveAttribute("href", `${repositoryUrl}/blob/main/docs/support-matrix.md#validation-boundaries`);
  await expect(page.getByRole("link", { name: "HTTP interfaces", exact: true })).toHaveAttribute("href", `${repositoryUrl}/blob/main/docs/public-contracts.md#http-interfaces`);
  for (const [label, fragment] of [
    ["Work planning and human decisions", "work-planning"],
    ["Workflow execution", "workflow-execution"],
    ["Run inspection and token usage", "run-inspection"],
    ["Projects and team access", "project-access"],
    ["Project knowledge", "project-knowledge"],
    ["Code review in GitHub", "code-review"],
    ["Deployment previews and QA", "preview-qa"],
    ["Discord collaboration", "discord-collaboration"],
    ["Scheduled team briefings", "team-briefings"],
  ]) {
    const anchor = page.getByRole("navigation", { name: "Feature navigation" }).getByRole("link", { name: label, exact: true });
    await expect(anchor).toHaveAttribute("href", `#${fragment}`);
    await anchor.click();
    await expect(page).toHaveURL(new RegExp(`#${fragment}$`));
    await expect(page.locator(`#${fragment}`).getByRole("heading", { name: label, exact: true })).toBeInViewport();
  }
  await expect(page.getByRole("link", { name: "Complete quick start", exact: true })).toHaveAttribute("href", `${repositoryUrl}/blob/main/README.md#quick-start`);
  await expect(page.getByText("curl --fail http://localhost:60001/health", { exact: false })).toBeVisible();
  await expect(page.getByText(/Isolate execution environments/)).toBeVisible();
});


test("section navigation exposes delivery flow, worker requirements and local setup", async ({ page }) => {
  await page.route(githubApiUrl, (route) => route.fulfill({ status: 404, json: { message: "Not Found" } }));
  await page.goto("/master-builder/");
  await page.getByRole("navigation", { name: "Main navigation" }).getByRole("link", { name: "How it works", exact: true }).click();
  await expect(page).toHaveURL(/#architecture$/);
  await expect(page.getByRole("heading", { name: "From work item to reviewed change", exact: true })).toBeInViewport();
  await expect(page.getByRole("list", { name: "Delivery workflow" })).toBeVisible();

  await page.getByRole("navigation", { name: "Feature navigation" }).getByRole("link", { name: "Workflow execution", exact: true }).click();
  await expect(page).toHaveURL(/#workflow-execution$/);
  await expect(page.locator("#workflow-execution").getByRole("heading", { name: "Workflow execution", exact: true })).toBeInViewport();
  await expect(page.getByRole("link", { name: "Runtime requirements", exact: true })).toBeVisible();

  await page.getByRole("link", { name: "Local setup", exact: true }).click();
  await expect(page).toHaveURL(/#source-setup$/);
  await expect(page.getByRole("heading", { name: "Source setup", exact: true })).toBeInViewport();
  await expect(page.getByRole("link", { name: "Complete quick start", exact: true })).toBeVisible();
});


test("homepage feature catalogue explains user tasks across the product", async ({ page }, testInfo) => {
  await page.route(githubApiUrl, (route) => route.fulfill({ status: 404, json: { message: "Not Found" } }));
  await page.goto("/master-builder/");
  await expect(page.getByRole("heading", { name: "What’s included", exact: true })).toBeVisible();
  const catalogue = page.getByRole("navigation", { name: "Feature navigation" });
  for (const [name, id] of [
    ["Work planning and human decisions", "work-planning"],
    ["Workflow execution", "workflow-execution"],
    ["Run inspection and token usage", "run-inspection"],
    ["Projects and team access", "project-access"],
    ["Project knowledge", "project-knowledge"],
    ["Code review in GitHub", "code-review"],
    ["Deployment previews and QA", "preview-qa"],
    ["Discord collaboration", "discord-collaboration"],
    ["Scheduled team briefings", "team-briefings"],
  ]) {
    await catalogue.getByRole("link", { name, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`#${id}$`));
    const feature = page.locator(`#${id}`);
    await expect(feature.getByRole("heading", { name, exact: true })).toBeInViewport();
    await expect(feature.getByRole("link", { name: "Feature details", exact: true })).toHaveAttribute("href", `${repositoryUrl}/blob/main/docs/features.md#${id}`);
  }
  await expect(page.locator("#project-knowledge")).toContainText(/Upload project documents/);
  await expect(page.locator("#project-access")).toContainText(/invite members/);
  await expect(page.locator("#run-inspection")).toContainText(/input, cached and output token usage/);
  await expect(page.locator("#team-briefings")).toContainText(/stand-up and retrospective voice briefings/);
  await expect(page.locator("#team-briefings")).toContainText("Experimental integration");
  await expect(page.locator("#preview-qa")).toContainText("Experimental integration");
  await expect(page.getByRole("table", { name: "Master Builder component responsibilities" })).toHaveCount(0);
  await page.getByRole("navigation", { name: "Main navigation" }).getByRole("link", { name: "How it works", exact: true }).click();
  await expect(page.getByRole("heading", { name: "From work item to reviewed change", exact: true })).toBeInViewport();
  await expect(page.getByRole("list", { name: "Delivery workflow" }).getByRole("listitem")).toHaveCount(5);
  await page.getByRole("navigation", { name: "Main navigation" }).getByRole("link", { name: "Features", exact: true }).click();
  await expect(page.getByRole("heading", { name: "What’s included", exact: true })).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath("feature-catalogue.png") });
});
