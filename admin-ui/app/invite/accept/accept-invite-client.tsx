"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { useAuth } from "@/components/auth-provider";
import { getDefaultAuthenticatedRoute } from "@/lib/auth-routing";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { acceptPublicInvite } from "@/lib/api";

export function AcceptInviteClient() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { credentials, ready, login, needsOnboarding, principal } = useAuth();
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (ready && credentials && principal) {
      router.replace(needsOnboarding ? "/get-started" : getDefaultAuthenticatedRoute(principal));
    }
  }, [credentials, needsOnboarding, principal, ready, router]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const token = searchParams.get("token") ?? "";
    if (!token) {
      setErrorMessage("Invite token is missing.");
      return;
    }
    setSubmitting(true);
    setErrorMessage(null);
    try {
      const accepted = await acceptPublicInvite({
        token,
        password,
        full_name: fullName.trim() || null,
      });
      const email = accepted.principal.email;
      if (!email) {
        throw new Error("Invite acceptance succeeded but email was missing from the response.");
      }
      await login({ identifier: email, password });
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Unable to accept invite");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-950 px-4 py-8">
      <div className="w-full max-w-md rounded-2xl border border-white/10 bg-white/5 p-8 shadow-2xl backdrop-blur-xl">
        <h1 className="text-xl font-semibold text-white">Accept your invite</h1>
        <p className="mt-2 text-sm text-slate-400">Create your password and finish joining the tenant.</p>
        <form className="mt-6 space-y-4" onSubmit={handleSubmit}>
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-slate-300" htmlFor="full_name">
              Full name
            </label>
            <Input
              id="full_name"
              value={fullName}
              onChange={(event) => setFullName(event.target.value)}
              className="border-white/10 bg-white/10 text-white placeholder:text-slate-500"
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-slate-300" htmlFor="password">
              Password
            </label>
            <Input
              id="password"
              type="password"
              minLength={8}
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              className="border-white/10 bg-white/10 text-white placeholder:text-slate-500"
            />
          </div>
          {errorMessage ? (
            <p
              className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-400"
              role="alert"
            >
              {errorMessage}
            </p>
          ) : null}
          <Button className="w-full" type="submit" disabled={submitting}>
            {submitting ? "Joining..." : "Accept invite"}
          </Button>
        </form>
      </div>
    </main>
  );
}
