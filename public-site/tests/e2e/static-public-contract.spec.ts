import { expect, test } from "@playwright/test";

test("public website has no account session or sign-in entrypoint", async ({ page, context }) => {
  await page.route("https://api.github.com/repos/thedarkcder/master-builder", route => route.fulfill({status:404,json:{message:"Not Found"}}));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Open-source software factory", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign in", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Open dashboard", exact: true })).toHaveCount(0);
  expect(await context.cookies()).toEqual([]);
});

test("homepage and privacy need no cookies and expose no application session requests", async ({ page, context }) => {
  const originRequests: string[] = [];
  page.on("request", request => {
    if (new URL(request.url()).hostname === "127.0.0.1") originRequests.push(new URL(request.url()).pathname);
  });
  await page.route("https://api.github.com/repos/thedarkcder/master-builder", route => route.fulfill({ status: 404, json: { message: "Not Found" } }));
  const response = await page.goto("/");
  expect(response?.headers()["set-cookie"]).toBeUndefined();
  await page.getByRole("link", { name: "Privacy", exact: true }).click();
  await expect(page).toHaveURL(/\/privacy\/$/);
  await expect(page.getByRole("heading", { name: "Public website privacy" })).toBeVisible();
  await expect(page.getByText(/no accounts, login sessions, application cookies or analytics trackers/)).toBeVisible();
  await page.getByRole("link", { name: "Back to Master Builder" }).click();
  await expect(page).toHaveURL(/\/$/);
  expect(originRequests.some(path => path.includes("/api/"))).toBe(false);
  expect(await context.cookies()).toEqual([]);
});

for (const path of ["login", "dashboard", "api/auth/session", "api/platform/runs", "blog", "licenses/unlisted-file.txt"]) {
  test(`static site does not publish ${path}`, async ({ request }) => {
    const response = await request.get(`/${path}`, { maxRedirects: 0 });
    expect(response.status()).toBe(404);
    expect(response.headers().location).toBeUndefined();
    expect(response.headers()["set-cookie"]).toBeUndefined();
  });
}

test("built scripts, styles and fonts load from the public site root", async ({ page }) => {
  const assetResponses: { path: string; status: number }[] = [];
  page.on("response", response => {
    const url = new URL(response.url());
    if (url.hostname === "127.0.0.1" && /\.(?:js|css|woff2)$/.test(url.pathname)) {
      assetResponses.push({ path: url.pathname, status: response.status() });
    }
  });
  await page.route("https://api.github.com/repos/thedarkcder/master-builder", route => route.fulfill({ status: 404, json: { message: "Not Found" } }));
  await page.goto("/");
  await expect(page.getByRole("link", { name: "Star on GitHub" })).toContainText("Stars unavailable");
  await page.evaluate(() => document.fonts.ready);
  for (const extension of [".js", ".css", ".woff2"]) expect(assetResponses.some(asset => asset.path.endsWith(extension))).toBe(true);
  for (const asset of assetResponses) {
    expect(asset.path).toMatch(/^\/_next\//);
    expect(asset.status).toBe(200);
  }
  expect(await page.locator("body").evaluate(element => getComputedStyle(element).fontFamily)).toContain("Manrope");
});

test("project overview and source setup remain usable without JavaScript", async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  await page.goto("http://127.0.0.1:" + (process.env.PUBLIC_SITE_TEST_PORT ?? "4603") + "/");
  await expect(page.getByRole("heading", { name: "Open-source software factory", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "What’s included", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Get Started", exact: true }).first()).toHaveAttribute("href", "https://github.com/thedarkcder/master-builder");
  await page.getByRole("link", { name: "Privacy", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Public website privacy" })).toBeVisible();
  await context.close();
});
