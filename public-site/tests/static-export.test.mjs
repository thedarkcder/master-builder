import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile, readdir } from "node:fs/promises";
import { test } from "node:test";
import { BASE_PATH } from "../site.config.mjs";

const output = new URL("../out/", import.meta.url);
const source = new URL("../", import.meta.url);

test("exported homepage and privacy are complete static documents without session entrypoints", async () => {
  const home = await readFile(new URL("index.html", output), "utf8");
  const privacy = await readFile(new URL("privacy/index.html", output), "utf8");
  assert.match(home, /Open-source software factory/);
  assert.match(home, /What’s included/);
  assert.match(privacy, /Public website privacy/);
  for (const html of [home, privacy]) {
    assert.doesNotMatch(html, /next-auth|authjs\.session-token|api\/auth|ORCHESTRATOR_API_BASE_URL|AUTH_SECRET/);
    assert.doesNotMatch(html, /href="[^\"]*\/(?:login|register|dashboard)(?:\/|\")/);
  }
});

test("HTML script and stylesheet URLs use the project base path", async () => {
  for (const name of ["index.html", "privacy/index.html"]) {
    const html = await readFile(new URL(name, output), "utf8");
    const assets = [...html.matchAll(/(?:src|href)="([^\"]*\/_next\/[^\"]+)"/g)];
    assert.ok(assets.length > 0, "Expected emitted scripts and styles");
    for (const [, path] of assets) assert.ok(path.startsWith(`${BASE_PATH}/_next/`), path);
  }
  assert.equal(await readFile(new URL(".nojekyll", output), "utf8"), "");
});

test("export contains no application routes, environment files or runtime server bundle", async () => {
  const topLevel = await readdir(output);
  for (const forbidden of ["api", "login", "register", "dashboard", "blog", "server", ".env", ".env.local"]) {
    assert.ok(!topLevel.includes(forbidden), `${forbidden} must not be published`);
  }
  assert.ok(topLevel.includes("404.html"), "A static error page is required");
});

test("package contains no auth or dashboard runtime dependencies", async () => {
  const manifest = JSON.parse(await readFile(new URL("package.json", source), "utf8"));
  assert.equal(manifest.license, "AGPL-3.0-only");
  assert.deepEqual(Object.keys(manifest.dependencies).sort(), ["lucide-react", "next", "react", "react-dom"]);
  const lock = JSON.parse(await readFile(new URL("package-lock.json", source), "utf8"));
  for (const entry of Object.values(lock.packages)) {
    if (entry.resolved) assert.ok(entry.resolved.startsWith("https://registry.npmjs.org/"), "Dependencies must be publicly accessible");
  }
});

test("distributed font notices preserve canonical license and copyright bytes", async () => {
  for (const kind of ["OFL", "NOTICE"]) {
    const canonical = await readFile(new URL(`../../third_party/licenses/manrope-4.504/${kind}.txt`, import.meta.url));
    assert.deepEqual(await readFile(new URL(`licenses/manrope-${kind}.txt`, output)), canonical);
    assert.deepEqual(await readFile(new URL(`public/licenses/manrope-${kind}.txt`, source)), canonical);
  }
});

test("every distributed font matches the reviewed Manrope evidence", async () => {
  const evidence = JSON.parse(await readFile(new URL("../../third_party/distribution-evidence.json", import.meta.url), "utf8"));
  const reviewed = evidence.manrope_ui_font.built_woff2;
  const media = new URL("_next/static/media/", output);
  const actual = (await readdir(media)).filter(file => file.endsWith(".woff2")).sort();
  assert.deepEqual(actual, reviewed.map(font => font.filename).sort());
  for (const font of reviewed) {
    const bytes = await readFile(new URL(font.filename, media));
    assert.equal(bytes.length, font.bytes);
    assert.equal(createHash("sha256").update(bytes).digest("hex"), font.sha256);
  }
});
