import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.dirname(fileURLToPath(import.meta.url));

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
  outputFileTracingRoot: projectRoot,
  images: {
    remotePatterns: [
      {
        protocol: "https",
        hostname: "lh3.googleusercontent.com",
        pathname: "/aida-public/**"
      }
    ]
  },
  async redirects() {
    return [
      { source: "/dashboard", destination: "/platform/dashboard", permanent: true },
      { source: "/status", destination: "/platform/status", permanent: true },
      { source: "/agent-runtimes", destination: "/platform/agent-runtimes", permanent: true },
      { source: "/secrets", destination: "/platform/secrets", permanent: true },
    ];
  }
};

export default nextConfig;
