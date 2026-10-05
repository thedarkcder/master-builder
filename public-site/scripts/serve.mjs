// Local preview of the exact exported files, at the public domain root.
// Unknown files return 404; this is not an SPA or application server.
import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { dirname, extname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../out");
await stat(join(root, "index.html"));
await stat(join(root, "404.html"));
const rawPort = process.env.PUBLIC_SITE_PORT ?? "60003";
if (!/^\d+$/.test(rawPort) || Number(rawPort) < 1 || Number(rawPort) > 65535) {
  throw new Error("PUBLIC_SITE_PORT must be an integer between 1 and 65535");
}
const types = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
  ".woff2": "font/woff2",
  ".ico": "image/x-icon",
  ".svg": "image/svg+xml",
};

const server = createServer(async (request, response) => {
  if (request.method !== "GET" && request.method !== "HEAD") {
    response.writeHead(405, { Allow: "GET, HEAD" }).end();
    return;
  }
  let path;
  try {
    path = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
  } catch {
    response.writeHead(400).end("Invalid URL");
    return;
  }
  if (path.includes("\\") || path.includes("\0") || path.split("/").includes("..")) {
    response.writeHead(404).end("Not found");
    return;
  }
  const relative = path.slice(1);
  const file = resolve(root, relative.endsWith("/") || relative === "" ? `${relative}index.html` : relative);
  if (!file.startsWith(`${root}${sep}`)) {
    response.writeHead(404).end("Not found");
    return;
  }
  try {
    const body = await readFile(file);
    response.writeHead(200, { "Content-Type": types[extname(file)] ?? "application/octet-stream", "X-Content-Type-Options": "nosniff" });
    response.end(request.method === "HEAD" ? undefined : body);
  } catch (error) {
    if (error.code !== "ENOENT" && error.code !== "ENOTDIR" && error.code !== "EISDIR") {
      console.error("Static preview could not read an exported file:", error.code);
      response.writeHead(500).end("Static file read failed");
      return;
    }
    response.writeHead(404, { "Content-Type": "text/html; charset=utf-8", "X-Content-Type-Options": "nosniff" });
    const notFound = await readFile(join(root, "404.html"));
    response.end(request.method === "HEAD" ? undefined : notFound);
  }
});
server.listen(Number(rawPort), "127.0.0.1", () => console.log(`Static preview: http://127.0.0.1:${rawPort}/`));
