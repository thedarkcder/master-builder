import { BASE_PATH } from "./site.config.mjs";
import { fileURLToPath } from "node:url";
/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "export",
  basePath: BASE_PATH,
  trailingSlash: true,
  turbopack: { root: fileURLToPath(new URL(".", import.meta.url)) },
};
export default nextConfig;
