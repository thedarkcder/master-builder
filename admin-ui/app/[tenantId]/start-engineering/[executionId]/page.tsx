"use client";

import Link from "next/link";
import { Suspense, useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { ArrowRight, CheckCircle2, Loader2, LockKeyhole, Play, ShieldCheck } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { useToast } from "@/components/ui/toast-provider";
import {
  getStartEngineeringPreview,
  startEngineeringFromAction,
  type StartEngineeringPreviewRecord,
  type WorkflowStartWorkResponseRecord,
} from "@/lib/api";
import { cn } from "@/lib/utils";

export default function StartEngineeringPage() {
  return (
    <Suspense fallback={<StartEngineeringShell state="loading" />}>
      <StartEngineeringPageInner />
    </Suspense>
  );
}

function StartEngineeringPageInner() {
  const params = useParams<{ tenantId: string; executionId: string }>();
  const searchParams = useSearchParams();
  const { credentials, principalReady, ready } = useAuth();
  const { showToast } = useToast();
  const tenantId = decodeURIComponent(params.tenantId);
  const executionId = decodeURIComponent(params.executionId);
  const actionToken = searchParams.get("startDevelopmentToken")?.trim() || "";
  const [preview, setPreview] = useState<StartEngineeringPreviewRecord | null>(null);
  const [result, setResult] = useState<WorkflowStartWorkResponseRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const currentUrl = useMemo(() => {
    if (typeof window === "undefined") return `/${encodeURIComponent(tenantId)}/start-engineering/${encodeURIComponent(executionId)}`;
    return `${window.location.pathname}${window.location.search}`;
  }, [executionId, tenantId]);

  useEffect(() => {
    let disposed = false;
    async function loadPreview() {
      if (!ready) return;
      if (!credentials) {
        setLoading(false);
        return;
      }
      if (!principalReady) return;
      if (!actionToken) {
        setError("This start link is missing its signed action token.");
        setLoading(false);
        return;
      }
      setLoading(true);
      setError(null);
      try {
        const nextPreview = await getStartEngineeringPreview(credentials, executionId, actionToken);
        if (!disposed) setPreview(nextPreview);
      } catch (loadError) {
        if (!disposed) setError((loadError as Error).message);
      } finally {
        if (!disposed) setLoading(false);
      }
    }
    void loadPreview();
    return () => {
      disposed = true;
    };
  }, [actionToken, credentials, executionId, principalReady, ready]);

  async function handleStart() {
    if (!credentials || !preview || !actionToken) return;
    setStarting(true);
    setError(null);
    try {
      const response = await startEngineeringFromAction(credentials, executionId, actionToken);
      setResult(response);
      showToast({
        title: "Engineering work started",
        description: `${response.queued.length} queued, ${response.skipped.length} already active or skipped.`,
        tone: "success",
      });
    } catch (startError) {
      setError((startError as Error).message);
      showToast({ title: "Start failed", description: (startError as Error).message, tone: "error" });
    } finally {
      setStarting(false);
    }
  }

  if (!ready) {
    return <StartEngineeringShell state="loading" />;
  }

  if (!credentials) {
    return (
      <StartEngineeringShell state="auth">
        <Link
          href={`/login?next=${encodeURIComponent(currentUrl)}`}
          className="inline-flex h-11 items-center justify-center rounded-full bg-slate-950 px-5 text-sm font-semibold text-white transition hover:bg-slate-800"
        >
          Sign in to continue
          <ArrowRight className="ml-2 h-4 w-4" />
        </Link>
      </StartEngineeringShell>
    );
  }

  if (loading) {
    return <StartEngineeringShell state="loading" />;
  }

  return (
    <main className="min-h-screen bg-[#f5f1e8] px-5 py-8 text-slate-950 sm:px-8 lg:px-12">
      <div className="mx-auto flex min-h-[calc(100vh-4rem)] w-full max-w-5xl flex-col justify-between">
        <header className="flex items-center justify-between border-b border-slate-950/10 pb-5">
          <Link href="/" className="text-sm font-bold tracking-tight">Master Builder</Link>
          <span className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Jira action</span>
        </header>

        <section className="grid gap-10 py-12 lg:grid-cols-[1.1fr_0.9fr] lg:items-end">
          <div>
            <p className="mb-4 inline-flex items-center gap-2 rounded-full border border-slate-950/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.16em] text-slate-600">
              <ShieldCheck className="h-3.5 w-3.5" />
              Signed Jira request
            </p>
            <h1 className="max-w-3xl text-5xl font-black tracking-[-0.06em] sm:text-6xl lg:text-7xl">
              Start engineering work
            </h1>
            <p className="mt-6 max-w-xl text-base leading-7 text-slate-600">
              This starts ready engineering child tickets for the Jira parent. Master Builder skips anything already active.
            </p>
          </div>

          <div className="border-l border-slate-950/10 pl-6">
            <div className="space-y-5">
              <div>
                <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Parent issue</p>
                <p className="mt-2 text-2xl font-bold">{preview?.issue_key ?? "—"}</p>
                <p className="mt-1 text-sm text-slate-600">{preview?.display_name ?? "Prepared engineering work"}</p>
              </div>
              <div className="flex items-center gap-3">
                <StatusBadge status={preview?.workflow_status ?? "unknown"} />
                <span className="text-sm text-slate-600">{preview?.can_start ? "Ready to start" : preview?.unavailable_reason}</span>
              </div>
            </div>
          </div>
        </section>

        <section className="border-t border-slate-950/10 py-8">
          {error ? (
            <p className="mb-5 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-700" role="alert">
              {error}
            </p>
          ) : null}

          {result ? (
            <div className="space-y-5">
              <div className="flex items-center gap-3 text-emerald-700">
                <CheckCircle2 className="h-6 w-6" />
                <p className="text-lg font-bold">Engineering work has been started.</p>
              </div>
              <IssueResultList result={result} />
            </div>
          ) : (
            <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
              <p className="max-w-xl text-sm text-slate-600">
                Use this once the team is ready for the AI engineering engine to pick up prepared work.
              </p>
              <Button
                className="h-12 rounded-full bg-slate-950 px-6 text-white hover:bg-slate-800"
                disabled={!preview?.can_start || starting}
                onClick={() => void handleStart()}
              >
                {starting ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
                Start ready engineering work
              </Button>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}

function StartEngineeringShell({ state, children }: { state: "loading" | "auth"; children?: React.ReactNode }) {
  return (
    <main className="flex min-h-screen items-center justify-center bg-[#f5f1e8] px-5 text-slate-950">
      <div className="max-w-lg text-center">
        <div className="mx-auto mb-6 flex h-14 w-14 items-center justify-center rounded-full bg-slate-950 text-white">
          {state === "loading" ? <Loader2 className="h-5 w-5 animate-spin" /> : <LockKeyhole className="h-5 w-5" />}
        </div>
        <h1 className="text-3xl font-black tracking-[-0.04em]">
          {state === "loading" ? "Checking start link" : "Sign in required"}
        </h1>
        <p className="mt-3 text-sm leading-6 text-slate-600">
          {state === "loading"
            ? "Master Builder is validating the signed Jira action."
            : "Sign in to your workspace before starting engineering work."}
        </p>
        {children ? <div className="mt-6">{children}</div> : null}
      </div>
    </main>
  );
}

function IssueResultList({ result }: { result: WorkflowStartWorkResponseRecord }) {
  const rows = [...result.queued, ...result.skipped];
  return (
    <div className="divide-y divide-slate-950/10 border-y border-slate-950/10">
      {rows.map((item) => (
        <div key={`${item.issue_key}:${item.run_id ?? item.reason ?? item.status}`} className="flex items-center justify-between gap-4 py-3 text-sm">
          <span className="font-semibold">{item.issue_key}</span>
          <span className={cn("text-slate-600", item.reason ? "text-amber-700" : "text-emerald-700")}>
            {item.reason ?? item.status}
          </span>
        </div>
      ))}
    </div>
  );
}
