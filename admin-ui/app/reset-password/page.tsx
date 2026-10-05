"use client";

import { PUBLIC_SITE_URL } from "@/lib/public-site";

import Link from "next/link";
import { FormEvent, Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { confirmPasswordReset } from "@/lib/api";

export default function ResetPasswordPage() {
  return (
    <Suspense fallback={<ResetPasswordFallback />}>
      <ResetPasswordPageInner />
    </Suspense>
  );
}

function ResetPasswordFallback() {
  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-gradient-to-br from-slate-950 via-indigo-950 to-slate-900 px-4 py-8">
      <p className="text-sm text-slate-400">Loading…</p>
    </main>
  );
}

function ResetPasswordPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [nextPassword, setNextPassword] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const token = searchParams.get("token") ?? "";
    if (!token) {
      setErrorMessage("Password reset token is missing.");
      return;
    }
    setSubmitting(true);
    setErrorMessage(null);
    try {
      await confirmPasswordReset({
        token,
        new_password: nextPassword,
      });
      router.replace("/login?reset=success");
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Unable to reset password");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-gradient-to-br from-slate-950 via-indigo-950 to-slate-900 px-4 py-8">
      <div className="relative w-full max-w-md rounded-2xl border border-white/10 bg-white/5 p-8 shadow-2xl backdrop-blur-xl">
        <Link href={PUBLIC_SITE_URL} className="inline-flex items-center text-sm text-slate-300 underline-offset-2 hover:text-white hover:underline">
          Public website
        </Link>
        <h1 className="mt-6 text-xl font-semibold text-white">Choose a new password</h1>
        <p className="mt-2 text-sm text-slate-400">Set a new password for your Master Builder account.</p>

        <form className="mt-6 space-y-4" onSubmit={handleSubmit}>
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-slate-300" htmlFor="newPassword">
              New password
            </label>
            <Input
              id="newPassword"
              type="password"
              minLength={8}
              value={nextPassword}
              onChange={(event) => setNextPassword(event.target.value)}
              required
              className="border-white/10 bg-white/10 text-white placeholder:text-slate-500 focus-visible:ring-indigo-500"
            />
          </div>

          {errorMessage ? (
            <p className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300" role="alert">
              {errorMessage}
            </p>
          ) : null}

          <Button className="w-full bg-indigo-600 text-white hover:bg-indigo-500" type="submit" disabled={submitting}>
            {submitting ? "Updating..." : "Reset password"}
          </Button>
        </form>

        <p className="mt-6 text-center text-sm text-slate-400">
          <Link href="/login" className="text-indigo-300 underline-offset-2 hover:underline">
            Back to sign in
          </Link>
        </p>
      </div>
    </main>
  );
}
