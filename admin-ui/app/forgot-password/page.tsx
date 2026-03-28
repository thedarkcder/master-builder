"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { requestPasswordReset } from "@/lib/api";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [statusLine, setStatusLine] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setErrorMessage(null);
    setStatusLine(null);
    try {
      const response = await requestPasswordReset({ email: email.trim() });
      setStatusLine(response.detail);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Unable to request a reset link");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="relative flex min-h-screen items-center justify-center overflow-hidden bg-gradient-to-br from-slate-950 via-indigo-950 to-slate-900 px-4 py-8">
      <div className="relative w-full max-w-md rounded-2xl border border-white/10 bg-white/5 p-8 shadow-2xl backdrop-blur-xl">
        <Link href="/" className="inline-flex items-center text-sm text-slate-300 underline-offset-2 hover:text-white hover:underline">
          Back to home
        </Link>
        <h1 className="mt-6 text-xl font-semibold text-white">Reset password</h1>
        <p className="mt-2 text-sm text-slate-400">Enter your work email and we&apos;ll send a reset link.</p>

        <form className="mt-6 space-y-4" onSubmit={handleSubmit}>
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-slate-300" htmlFor="email">
              Work email
            </label>
            <Input
              id="email"
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
              className="border-white/10 bg-white/10 text-white placeholder:text-slate-500 focus-visible:ring-indigo-500"
            />
          </div>

          {statusLine ? (
            <p className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300">
              {statusLine}
            </p>
          ) : null}
          {errorMessage ? (
            <p className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300" role="alert">
              {errorMessage}
            </p>
          ) : null}

          <Button className="w-full bg-indigo-600 text-white hover:bg-indigo-500" type="submit" disabled={submitting}>
            {submitting ? "Sending..." : "Send reset link"}
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
