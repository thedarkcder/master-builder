"use client";

import { PUBLIC_SITE_URL } from "@/lib/public-site";

import { FormEvent, Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Zap } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { getDefaultAuthenticatedRoute } from "@/lib/auth-routing";
import { clearLogoutRedirectBarrier, hasLogoutRedirectBarrier } from "@/lib/auth-redirect-barrier";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { readLastWorkspaceTenantIdFromBrowser } from "@/lib/workspace-preference";
import { getLocalNavigationDestination, INVALID_LOCAL_NAVIGATION_DESTINATION } from "@/lib/local-navigation-destination";

export default function LoginPage() {
  return (
    <Suspense fallback={<LoginPageFallback />}>
      <LoginPageInner />
    </Suspense>
  );
}

function LoginPageFallback() {
  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-gradient-to-br from-slate-950 via-indigo-950 to-slate-900 px-4 py-8">
      <div className="relative w-full max-w-md">
        <p className="text-center text-sm text-slate-400">Loading…</p>
      </div>
    </main>
  );
}

function LoginPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { credentials, ready, login, needsOnboarding, principal } = useAuth();

  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const nextPath = searchParams.get("next");
  let destination: string | null = null;
  let destinationError: string | null = null;
  try {
    destination = getLocalNavigationDestination(nextPath);
  } catch {
    destinationError = INVALID_LOCAL_NAVIGATION_DESTINATION;
  }

  function normalizeAuthErrorMessage(message: string | null | undefined): string | null {
    if (!message) {
      return null;
    }
    if (message === "CredentialsSignin" || message === "Invalid tenant credentials") {
      return "Invalid credentials";
    }
    return message;
  }

  useEffect(() => {
    if (ready && !credentials) {
      clearLogoutRedirectBarrier();
    }
  }, [credentials, ready]);

  useEffect(() => {
    if (!destinationError && ready && credentials && principal) {
      if (hasLogoutRedirectBarrier()) {
        return;
      }
      const preferredTenantId = readLastWorkspaceTenantIdFromBrowser();
      router.replace(
        needsOnboarding
          ? "/get-started"
          : destination ?? getDefaultAuthenticatedRoute(principal, { preferredTenantId }),
      );
    }
  }, [credentials, destination, destinationError, needsOnboarding, principal, ready, router]);

  useEffect(() => {
    const authError = searchParams.get("error");
    const nextError = normalizeAuthErrorMessage(authError);
    if (nextError) {
      setErrorMessage(nextError);
    }
  }, [searchParams]);

  const resetSucceeded = searchParams.get("reset") === "success";

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (destinationError) return;
    setErrorMessage(null);
    setIsSubmitting(true);
    try {
      await login({
        identifier: identifier.trim(),
        password,
        redirectTo: destination,
      });
    } catch (error) {
      const message = normalizeAuthErrorMessage(error instanceof Error ? error.message : "Invalid credentials");
      setErrorMessage(message);
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-gradient-to-br from-slate-950 via-indigo-950 to-slate-900 px-4 py-8">
      {/* Decorative glow orbs */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute -left-40 -top-40 h-80 w-80 rounded-full bg-indigo-600/20 blur-3xl" />
        <div className="absolute -bottom-40 -right-40 h-80 w-80 rounded-full bg-violet-600/20 blur-3xl" />
      </div>

      <div className="relative w-full max-w-md">
        <div className="mb-6">
          <Link href={PUBLIC_SITE_URL} className="inline-flex items-center text-sm text-slate-300 underline-offset-2 hover:text-white hover:underline">
            Public website
          </Link>
        </div>

        {/* Logo + wordmark above card */}
        <div className="mb-8 flex flex-col items-center gap-3 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-indigo-600 shadow-[0_0_32px_rgba(99,102,241,0.5)] ring-1 ring-indigo-400/30">
            <Zap className="h-7 w-7 text-white" />
          </div>
          <div>
            <h1 className="text-2xl font-bold text-white">Master Builder</h1>
          </div>
        </div>

        {/* Frosted glass card */}
        <div className="rounded-2xl border border-white/10 bg-white/5 p-8 backdrop-blur-xl shadow-2xl">
          <h2 className="mb-1 text-lg font-semibold text-white">Sign in</h2>
          <p className="mb-6 text-sm text-slate-400">Use your email for tenant access or a username for platform administration.</p>

          <form className="space-y-4" onSubmit={handleSubmit}>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-slate-300" htmlFor="identifier">
                Email or username
              </label>
              <Input
                id="identifier"
                value={identifier}
                onChange={(e) => setIdentifier(e.target.value)}
                required
                className="border-white/10 bg-white/10 text-white placeholder:text-slate-500 focus-visible:ring-indigo-500"
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-slate-300" htmlFor="password">
                Password
              </label>
              <Input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                className="border-white/10 bg-white/10 text-white placeholder:text-slate-500 focus-visible:ring-indigo-500"
              />
            </div>

            <div className="flex justify-end">
              <Link href="/forgot-password" className="text-sm text-indigo-300 underline-offset-2 hover:underline">
                Forgot password?
              </Link>
            </div>

          {destinationError || errorMessage ? (
            <p className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-400" role="alert">
              {destinationError ?? errorMessage}
            </p>
          ) : null}
          {resetSucceeded ? (
            <p className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300">
              Password updated. Sign in with your new password.
            </p>
          ) : null}

          <Button
              className="w-full bg-indigo-600 text-white hover:bg-indigo-500 focus-visible:ring-indigo-500"
              type="submit"
              disabled={isSubmitting || destinationError !== null}
            >
              {isSubmitting ? "Signing in..." : "Sign in"}
            </Button>

            <p className="text-center text-sm text-slate-400">
              New to Master Builder?{" "}
              <Link href="/register" className="text-indigo-300 underline-offset-2 hover:underline">
                Create your workspace
              </Link>
            </p>

            <p className="text-center text-xs text-slate-500">
              <Link href="/privacy" className="text-indigo-400 underline-offset-2 hover:underline">
                Privacy Policy
              </Link>
            </p>
          </form>
        </div>
      </div>
    </main>
  );
}
