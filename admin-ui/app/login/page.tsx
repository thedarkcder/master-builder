"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Zap } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export default function LoginPage() {
  const router = useRouter();
  const { credentials, ready, login, needsOnboarding } = useAuth();

  const [identifier, setIdentifier] = useState("admin");
  const [password, setPassword] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    if (ready && credentials) {
      router.replace(needsOnboarding ? "/get-started" : "/dashboard");
    }
  }, [credentials, needsOnboarding, ready, router]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMessage(null);
    setIsSubmitting(true);
    try {
      await login({
        identifier: identifier.trim(),
        password
      });
      router.push("/dashboard");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Invalid credentials";
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
        {/* Logo + wordmark above card */}
        <div className="mb-8 flex flex-col items-center gap-3 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-indigo-600 shadow-[0_0_32px_rgba(99,102,241,0.5)] ring-1 ring-indigo-400/30">
            <Zap className="h-7 w-7 text-white" />
          </div>
          <div>
            <h1 className="text-2xl font-bold text-white">Master Builder</h1>
            <p className="text-sm text-indigo-300/80">AI Orchestration Platform</p>
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

            {errorMessage ? (
              <p className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-400" role="alert">
                {errorMessage}
              </p>
            ) : null}

            <Button
              className="w-full bg-indigo-600 text-white hover:bg-indigo-500 focus-visible:ring-indigo-500"
              type="submit"
              disabled={isSubmitting}
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
