// Server requests use runtime service networking, independently of browser URLs.
function requireServerApiBaseUrl(): string {
  const value = process.env.ORCHESTRATOR_API_BASE_URL?.trim();
  if (!value) {
    throw new Error("ORCHESTRATOR_API_BASE_URL must configure the UI server's backend URL");
  }
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("ORCHESTRATOR_API_BASE_URL must be an absolute HTTP(S) service URL");
  }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
    throw new Error("ORCHESTRATOR_API_BASE_URL must be an HTTP(S) origin without credentials, paths, queries or fragments");
  }
  return url.origin;
}

export const SERVER_API_BASE_URL = requireServerApiBaseUrl();
