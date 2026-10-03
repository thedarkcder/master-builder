"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { SessionProvider, signIn, signOut, useSession } from "next-auth/react";

import {
  readAuthenticatedPrincipal,
  type AuthenticatedPrincipalRecord,
  type Credentials
} from "@/lib/api";
import { clearLogoutRedirectBarrier, setLogoutRedirectBarrier } from "@/lib/auth-redirect-barrier";
import { getLocalNavigationDestination } from "@/lib/local-navigation-destination";

type AuthLoginInput = {
  identifier: string;
  password: string;
  redirectTo?: string | null;
};

type AuthContextValue = {
  credentials: Credentials | null;
  principal: AuthenticatedPrincipalRecord | null;
  principalReady: boolean;
  ready: boolean;
  needsOnboarding: boolean;
  login: (input: AuthLoginInput) => Promise<void>;
  logout: () => Promise<void>;
  refreshPrincipal: () => Promise<void>;
  applyPrincipal: (nextPrincipal: AuthenticatedPrincipalRecord | null) => void;
};

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

function hasPendingOnboarding(principal: AuthenticatedPrincipalRecord | null): boolean {
  if (!principal || principal.principal_type !== "tenant_user") {
    return false;
  }
  return principal.memberships.some((membership) => membership.onboarding_completed_at == null);
}

function AuthProviderInner({ children }: { children: React.ReactNode }) {
  const { status } = useSession();
  const [principal, setPrincipal] = useState<AuthenticatedPrincipalRecord | null>(null);
  const [principalReady, setPrincipalReady] = useState(false);
  const [sessionRevoked, setSessionRevoked] = useState(false);

  const credentials = useMemo<Credentials | null>(() => {
    if (status !== "authenticated" || sessionRevoked) {
      return null;
    }
    return {
      apiBaseUrl: ""
    };
  }, [sessionRevoked, status]);

  function normalizeAuthErrorMessage(message: string): string {
    if (message === "CredentialsSignin" || message === "Invalid tenant credentials") {
      return "Invalid credentials";
    }
    return message || "Invalid credentials";
  }

  function applyPrincipal(nextPrincipal: AuthenticatedPrincipalRecord | null): void {
    setSessionRevoked(false);
    setPrincipal(nextPrincipal);
    setPrincipalReady(true);
  }

  async function refreshPrincipal(nextCredentials: Credentials | null = credentials): Promise<void> {
    if (!nextCredentials) {
      applyPrincipal(null);
      return;
    }
    try {
      const nextPrincipal = await readAuthenticatedPrincipal(nextCredentials);
      applyPrincipal(nextPrincipal);
    } catch (error) {
      applyPrincipal(null);
      if (error instanceof Error && /^(401|403):/.test(error.message)) {
        setSessionRevoked(true);
        await signOut({ redirect: false });
        if (typeof window !== "undefined") {
          window.location.replace("/login");
        }
      }
    }
  }

  useEffect(() => {
    if (status !== "authenticated") {
      setSessionRevoked(false);
      setPrincipalReady(false);
      setPrincipal(null);
      return;
    }
    setPrincipalReady(false);
    void refreshPrincipal();
  }, [credentials, status]);

  const value = useMemo<AuthContextValue>(
    () => ({
      credentials,
      principal,
      principalReady,
      ready: status !== "loading",
      needsOnboarding: hasPendingOnboarding(principal),
      login: async ({ identifier, password, redirectTo }) => {
        const destination = getLocalNavigationDestination(redirectTo);
        setPrincipal(null);
        setPrincipalReady(false);
        setSessionRevoked(false);
        const result = await signIn("credentials", {
          identifier: identifier.trim(),
          password,
          redirect: false,
          redirectTo: "/",
        });
        if (!result || result.error) {
          throw new Error(normalizeAuthErrorMessage(result?.error || "Invalid credentials"));
        }
        clearLogoutRedirectBarrier();
        if (typeof window !== "undefined") {
          window.location.assign(destination ?? "/platform/dashboard");
        }
      },
      logout: async () => {
        setSessionRevoked(false);
        setPrincipal(null);
        setPrincipalReady(false);
        setLogoutRedirectBarrier();
        const result = await signOut({ redirect: false, redirectTo: "/login" });
        if (typeof window !== "undefined") {
          const redirectUrl = typeof result?.url === "string" ? result.url : "/login";
          window.location.replace(redirectUrl);
        }
      },
      refreshPrincipal,
      applyPrincipal,
    }),
    [credentials, principal, principalReady, status]
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
