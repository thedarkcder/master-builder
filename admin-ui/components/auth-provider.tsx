"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { getSession, SessionProvider, signIn, signOut, useSession } from "next-auth/react";

import {
  readAuthenticatedPrincipal,
  type AuthenticatedPrincipalRecord,
  type Credentials
} from "@/lib/api";

type AuthLoginInput = {
  identifier: string;
  password: string;
};

type AuthContextValue = {
  credentials: Credentials | null;
  principal: AuthenticatedPrincipalRecord | null;
  ready: boolean;
  needsOnboarding: boolean;
  login: (input: AuthLoginInput) => Promise<void>;
  logout: () => Promise<void>;
  refreshPrincipal: () => Promise<void>;
};

type SessionUserShape = Record<string, never>;

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

function hasPendingOnboarding(principal: AuthenticatedPrincipalRecord | null): boolean {
  if (!principal || principal.principal_type !== "tenant_user") {
    return false;
  }
  return principal.memberships.some((membership) => membership.onboarding_completed_at == null);
}

function AuthProviderInner({ children }: { children: React.ReactNode }) {
  const { data: session, status } = useSession();
  const [principal, setPrincipal] = useState<AuthenticatedPrincipalRecord | null>(null);
  const sessionUser = session?.user as SessionUserShape | undefined;

  const credentials = useMemo<Credentials | null>(() => {
    if (!sessionUser || status !== "authenticated") {
      return null;
    }
    return {
      apiBaseUrl: ""
    };
  }, [sessionUser, status]);

  async function refreshPrincipal(nextCredentials: Credentials | null = credentials): Promise<void> {
    if (!nextCredentials) {
      setPrincipal(null);
      return;
    }
    try {
      const nextPrincipal = await readAuthenticatedPrincipal(nextCredentials);
      setPrincipal(nextPrincipal);
    } catch {
      setPrincipal(null);
    }
  }

  useEffect(() => {
    if (status !== "authenticated") {
      setPrincipal(null);
      return;
    }
    void refreshPrincipal();
  }, [credentials, status]);

  const value = useMemo<AuthContextValue>(
    () => ({
      credentials,
      principal,
      ready: status !== "loading",
      needsOnboarding: hasPendingOnboarding(principal),
      login: async ({ identifier, password }) => {
        const result = await signIn("credentials", {
          identifier: identifier.trim(),
          password,
          redirect: false
        });
        if (!result || result.error) {
          throw new Error(result?.error || "Invalid credentials");
        }
        const nextSession = await getSession();
        const nextSessionUser = nextSession?.user as SessionUserShape | undefined;
        const nextCredentials = nextSessionUser ? { apiBaseUrl: "" } : null;
        await refreshPrincipal(nextCredentials);
      },
      logout: async () => {
        setPrincipal(null);
        await signOut({ redirect: false });
      },
      refreshPrincipal
    }),
    [credentials, principal, status]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  return (
    <SessionProvider>
      <AuthProviderInner>{children}</AuthProviderInner>
    </SessionProvider>
  );
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return context;
}
