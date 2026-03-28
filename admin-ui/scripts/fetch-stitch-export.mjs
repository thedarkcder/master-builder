/**
 * Fetches HTML and screenshot URLs for a Stitch screen and saves them under public/stitch/.
 * Requires STITCH_API_KEY (see @google/stitch-sdk README).
 *
 * Usage:
 *   cd admin-ui && STITCH_API_KEY=... node scripts/fetch-stitch-export.mjs [projectId] [screenId]
 *
 * Defaults match the Master Builder marketing screen export.
 */

import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const adminUiRoot = join(__dirname, "..");
const outDir = join(adminUiRoot, "public", "stitch");

const DEFAULT_PROJECT = "11889777642742606588";
const DEFAULT_SCREEN = "fb1a8fe13e844b5e949e050d1017c3dd";

function imageExtFromContentType(contentType) {
  const ct = (contentType ?? "").toLowerCase();
  if (ct.includes("png")) return "png";
  if (ct.includes("jpeg") || ct.includes("jpg")) return "jpg";
  if (ct.includes("webp")) return "webp";
  return "png";
}

async function fetchBytes(url) {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`GET ${url} failed: ${res.status} ${res.statusText}`);
  }
  const buf = Buffer.from(await res.arrayBuffer());
  return { buf, contentType: res.headers.get("content-type") };
}

async function main() {
  const projectId = process.argv[2] ?? DEFAULT_PROJECT;
  const screenId = process.argv[3] ?? DEFAULT_SCREEN;

  if (!process.env.STITCH_API_KEY) {
    console.error("STITCH_API_KEY is not set.");
    process.exit(1);
  }

  const { stitch } = await import("@google/stitch-sdk");
  const project = stitch.project(projectId);
  const screen = await project.getScreen(screenId);

  const htmlUrl = await screen.getHtml();
  const imageUrl = await screen.getImage();

  await mkdir(outDir, { recursive: true });

  const htmlFetch = await fetchBytes(htmlUrl);
  await writeFile(join(outDir, "screen.html"), htmlFetch.buf);

  const imageFetch = await fetchBytes(imageUrl);
  const imageExt = imageExtFromContentType(imageFetch.contentType);
  const imagePath = join(outDir, `screen.${imageExt}`);
  await writeFile(imagePath, imageFetch.buf);

  const meta = {
    projectId,
    screenId,
    title: "Master Builder - Strategic AI Advisory",
    htmlUrl,
    imageUrl,
    htmlContentType: htmlFetch.contentType,
    imageContentType: imageFetch.contentType,
    imageFile: `screen.${imageExt}`,
    fetchedAt: new Date().toISOString()
  };

  await writeFile(join(outDir, "export-meta.json"), JSON.stringify(meta, null, 2), "utf8");

  console.log(`Wrote ${join(outDir, "screen.html")}`);
  console.log(`Wrote ${imagePath}`);
  console.log(`Wrote ${join(outDir, "export-meta.json")}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
