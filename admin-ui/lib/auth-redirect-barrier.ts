const LOGOUT_REDIRECT_BARRIER_KEY = "auth:logout-redirect-barrier";

function getSessionStorage(): Storage | null {
  if (typeof window === "undefined") {
    return null;
  }
  return window.sessionStorage;
}

export function hasLogoutRedirectBarrier(): boolean {
  return getSessionStorage()?.getItem(LOGOUT_REDIRECT_BARRIER_KEY) === "1";
}

export function setLogoutRedirectBarrier(): void {
  getSessionStorage()?.setItem(LOGOUT_REDIRECT_BARRIER_KEY, "1");
}

export function clearLogoutRedirectBarrier(): void {
  getSessionStorage()?.removeItem(LOGOUT_REDIRECT_BARRIER_KEY);
}
