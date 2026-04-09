const LAST_WORKSPACE_COOKIE_NAME = "mb_last_workspace";
const LAST_WORKSPACE_COOKIE_TTL_SECONDS = 60 * 60 * 24 * 365;

function normalizeTenantId(value: string | null | undefined): string | null {
  const normalized = String(value ?? "").trim();
  if (!normalized || normalized.length > 200 || normalized.includes("/")) {
    return null;
  }
  return normalized;
}

function parseCookieValue(cookieHeader: string | null | undefined, name: string): string | null {
  const source = String(cookieHeader ?? "");
  if (!source) {
    return null;
  }
  const segments = source.split(";");
  for (const segment of segments) {
    const [rawName, ...rawValueParts] = segment.split("=");
    if (rawName?.trim() !== name) {
      continue;
    }
    const rawValue = rawValueParts.join("=").trim();
    if (!rawValue) {
      return null;
    }
    try {
      return decodeURIComponent(rawValue);
    } catch {
      return null;
    }
  }
  return null;
}

export function getLastWorkspaceCookieName(): string {
  return LAST_WORKSPACE_COOKIE_NAME;
}

export function readLastWorkspaceTenantId(cookieHeader: string | null | undefined): string | null {
  return normalizeTenantId(parseCookieValue(cookieHeader, LAST_WORKSPACE_COOKIE_NAME));
}

export function readLastWorkspaceTenantIdFromBrowser(): string | null {
  if (typeof document === "undefined") {
    return null;
  }
  return readLastWorkspaceTenantId(document.cookie);
}

export function persistLastWorkspaceTenantId(tenantId: string | null | undefined): void {
  if (typeof document === "undefined") {
    return;
  }
  const normalized = normalizeTenantId(tenantId);
  if (!normalized) {
    return;
  }
  document.cookie = [
    `${LAST_WORKSPACE_COOKIE_NAME}=${encodeURIComponent(normalized)}`,
    "Path=/",
    `Max-Age=${LAST_WORKSPACE_COOKIE_TTL_SECONDS}`,
    "SameSite=Lax",
  ].join("; ");
}
