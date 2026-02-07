"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";

import type { Credentials } from "@/lib/api";
import {
  AUTH_COOKIE_KEY,
  AUTH_COOKIE_TTL_SECONDS,
  AUTH_STORAGE_KEY,
  DEFAULT_API_BASE_URL
} from "@/lib/auth-constants";

type AuthContextValue = {
  credentials: Credentials | null;
  ready: boolean;
  login: (nextCredentials: Credentials) => void;
  logout: () => void;
};

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

function writeSessionCookie(enabled: boolean): void {
  if (typeof document === "undefined") {
    return;
  }
  if (!enabled) {
    document.cookie = `${AUTH_COOKIE_KEY}=; path=/; max-age=0; samesite=lax`;
    return;
  }
  document.cookie = `${AUTH_COOKIE_KEY}=1; path=/; max-age=${AUTH_COOKIE_TTL_SECONDS}; samesite=lax`;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [credentials, setCredentials] = useState<Credentials | null>(null);

  useEffect(() => {
    const raw = window.localStorage.getItem(AUTH_STORAGE_KEY);
    if (!raw) {
      setReady(true);
      return;
    }

    try {
      const parsed = JSON.parse(raw) as Credentials;
      if (parsed.apiBaseUrl && parsed.username && parsed.password) {
        setCredentials(parsed);
        writeSessionCookie(true);
      } else {
        window.localStorage.removeItem(AUTH_STORAGE_KEY);
        writeSessionCookie(false);
      }
    } catch {
      window.localStorage.removeItem(AUTH_STORAGE_KEY);
      writeSessionCookie(false);
    }

    setReady(true);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      credentials,
      ready,
      login: (nextCredentials: Credentials) => {
        const normalized: Credentials = {
          apiBaseUrl: nextCredentials.apiBaseUrl.trim() || DEFAULT_API_BASE_URL,
          username: nextCredentials.username.trim(),
          password: nextCredentials.password
        };
        window.localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(normalized));
        writeSessionCookie(true);
        setCredentials(normalized);
      },
      logout: () => {
        window.localStorage.removeItem(AUTH_STORAGE_KEY);
        writeSessionCookie(false);
        setCredentials(null);
      }
    }),
    [credentials, ready]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return context;
}
