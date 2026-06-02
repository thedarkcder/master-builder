import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";

const [, , inputPath, outputPath] = process.argv;
const require = createRequire(import.meta.url);

if (!inputPath || !outputPath) {
  throw new Error("usage: node qa_demo_recorder.mjs <input.json> <output.json>");
}

function loadPlaywright() {
  const moduleDir = String(process.env.QA_DEMO_PLAYWRIGHT_MODULE_DIR || "").trim();
  if (moduleDir) {
    return require(path.join(moduleDir, "playwright"));
  }
  return require("playwright");
}

const { chromium } = loadPlaywright();
const input = JSON.parse(await fs.readFile(inputPath, "utf8"));
const previewUrl = String(input.preview_url || "").trim();
if (!previewUrl) {
  throw new Error("preview_url is required");
}

await fs.mkdir(input.output_dir, { recursive: true });

const recordings = [];

async function resolveLocator(page, selector) {
  if (!selector) {
    throw new Error("selector is required");
  }
  if (selector.startsWith("text=")) {
    const text = selector.slice(5);
    const exact = page.getByText(text, { exact: true });
    if ((await exact.count()) === 1) {
      return exact;
    }
  }
  return page.locator(selector);
}

async function resolveTextLocator(page, text) {
  const exact = page.getByText(text, { exact: true });
  if ((await exact.count()) === 1) {
    return exact;
  }
  return page.getByText(text, { exact: false }).first();
}

for (const scenario of input.scenarios || []) {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    recordVideo: {
      dir: input.output_dir,
      size: { width: 1440, height: 900 },
    },
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();
  const video = page.video();
  const startPath = String(scenario.start_path || "/");
  await page.goto(new URL(startPath, previewUrl).toString(), { waitUntil: "networkidle" });
  for (const step of scenario.steps || []) {
    const action = String(step.action || "").trim();
    const selector = step.selector ? String(step.selector) : null;
    const value = step.value ? String(step.value) : null;
    if (action === "goto") {
      await page.goto(new URL(value || "/", previewUrl).toString(), { waitUntil: "networkidle" });
      continue;
    }
    if (action === "click") {
      await (await resolveLocator(page, selector)).click();
      continue;
    }
    if (action === "fill") {
      await (await resolveLocator(page, selector)).fill(value || "");
      continue;
    }
    if (action === "press") {
      await (await resolveLocator(page, selector)).press(value || "Enter");
      continue;
    }
    if (action === "select_option") {
      await (await resolveLocator(page, selector)).selectOption(value || "");
      continue;
    }
    if (action === "wait_for_text") {
      await (await resolveTextLocator(page, value || "")).waitFor();
      continue;
    }
    if (action === "wait_for_url") {
      await page.waitForURL(new URL(value || "/", previewUrl).toString());
      continue;
    }
    if (action === "assert_text") {
      const text = await (await resolveLocator(page, selector)).textContent();
      if (!text || !text.includes(value || "")) {
        throw new Error(`assert_text failed for ${selector}`);
      }
      continue;
    }
    if (action === "assert_visible") {
      await (await resolveLocator(page, selector)).waitFor({ state: "visible" });
      continue;
    }
    throw new Error(`Unsupported QA step action: ${action}`);
  }
  await context.close();
  const videoPath = await video.path();
  await browser.close();
  const targetPath = path.join(input.output_dir, `${String(scenario.name || "demo").replace(/[^a-z0-9]+/gi, "-").toLowerCase()}.webm`);
  await fs.copyFile(videoPath, targetPath);
  recordings.push({ name: String(scenario.name || "Demo"), path: targetPath });
}

await fs.writeFile(outputPath, JSON.stringify({ recordings }, null, 2), "utf8");
