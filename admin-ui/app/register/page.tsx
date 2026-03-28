"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Rocket } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { getDefaultAuthenticatedRoute } from "@/lib/auth-routing";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { registerTenantAdministrator } from "@/lib/api";

export default function RegisterPage() {
  const router = useRouter();
  const { credentials, ready, needsOnboarding, login, principal } = useAuth();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [tenantName, setTenantName] = useState("");
  const [password, setPassword] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    if (ready && credentials && principal) {
      router.replace(needsOnboarding ? "/get-started" : getDefaultAuthenticatedRoute(principal));
    }
  }, [credentials, needsOnboarding, principal, ready, router]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMessage(null);
    setIsSubmitting(true);
    try {
      await registerTenantAdministrator({
        full_name: fullName.trim(),
        email: email.trim(),
        password,
        tenant_name: tenantName.trim()
      });
      await login({ identifier: email.trim(), password });
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Unable to create workspace");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-[radial-gradient(circle_at_top_left,_rgba(245,158,11,0.22),_transparent_35%),radial-gradient(circle_at_bottom_right,_rgba(59,130,246,0.22),_transparent_40%),linear-gradient(135deg,#0f172a,#111827,#1f2937)] px-4 py-8">
      <div className="relative w-full max-w-lg">
        <div className="mb-6">
          <Link href="/" className="inline-flex items-center text-sm text-slate-300 underline-offset-2 hover:text-white hover:underline">
            Back to home
          </Link>
        </div>

        <div className="mb-8 flex flex-col items-center gap-3 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-amber-500 shadow-[0_0_32px_rgba(245,158,11,0.45)] ring-1 ring-amber-300/30">
            <Rocket className="h-7 w-7 text-white" />
          </div>
          <div>
            <h1 className="text-2xl font-bold text-white">Launch a Master Builder tenant</h1>
            <p className="text-sm text-amber-100/80">Create your workspace and become the first tenant admin.</p>
          </div>
        </div>

        <div className="rounded-2xl border border-white/10 bg-white/5 p-8 shadow-2xl backdrop-blur-xl">
          <form className="space-y-4" onSubmit={handleSubmit}>
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5 sm:col-span-2">
                <label className="text-sm font-medium text-slate-200" htmlFor="fullName">
                  Full name
                </label>
                <Input
                  id="fullName"
                  value={fullName}
                  onChange={(event) => setFullName(event.target.value)}
                  required
                  className="border-white/10 bg-white/10 text-white placeholder:text-slate-500 focus-visible:ring-amber-500"
                />
              </div>
              <div className="space-y-1.5 sm:col-span-2">
                <label className="text-sm font-medium text-slate-200" htmlFor="email">
                  Work email
                </label>
                <Input
                  id="email"
                  type="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  required
                  className="border-white/10 bg-white/10 text-white placeholder:text-slate-500 focus-visible:ring-amber-500"
                />
              </div>
              <div className="space-y-1.5 sm:col-span-2">
                <label className="text-sm font-medium text-slate-200" htmlFor="tenantName">
                  Workspace name
                </label>
                <Input
                  id="tenantName"
                  value={tenantName}
                  onChange={(event) => setTenantName(event.target.value)}
                  required
                  className="border-white/10 bg-white/10 text-white placeholder:text-slate-500 focus-visible:ring-amber-500"
                />
              </div>
              <div className="space-y-1.5 sm:col-span-2">
                <label className="text-sm font-medium text-slate-200" htmlFor="password">
                  Password
                </label>
                <Input
                  id="password"
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  minLength={8}
                  required
                  className="border-white/10 bg-white/10 text-white placeholder:text-slate-500 focus-visible:ring-amber-500"
                />
              </div>
            </div>

            {errorMessage ? (
              <p className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300" role="alert">
                {errorMessage}
              </p>
            ) : null}

            <Button
              className="w-full bg-amber-500 text-slate-950 hover:bg-amber-400"
              type="submit"
              disabled={isSubmitting}
            >
              {isSubmitting ? "Creating workspace..." : "Create workspace"}
            </Button>

            <p className="text-center text-sm text-slate-400">
              Already have an account?{" "}
              <Link href="/login" className="text-amber-300 underline-offset-2 hover:underline">
                Sign in
              </Link>
            </p>
          </form>
        </div>
      </div>
    </main>
  );
}
