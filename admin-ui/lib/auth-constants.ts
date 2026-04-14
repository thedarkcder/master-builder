export const AUTH_STORAGE_KEY = "mb_admin_auth";
export const AUTH_COOKIE_KEY = "mb_admin_session";
export const AUTH_COOKIE_TTL_SECONDS = 60 * 60 * 8;

export const DEFAULT_API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:4000";
