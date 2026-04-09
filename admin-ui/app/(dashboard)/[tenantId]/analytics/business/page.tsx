"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { AlertCircle, CheckCircle2, Clock3, GitPullRequest } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Skeleton } from "@/components/ui/skeleton";
import { getTenantDeliverySummary, type DeliverySummaryRecord } from "@/lib/api";

export default function BusinessAnalyticsPage() {
  const { credentials } = useAuth();
  const params = useParams<{ tenantId: string }>();
  const tenantId = decodeURIComponent(params.tenantId);
  const [data, setData] = useState<DeliverySummaryRecord | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!credentials || !tenantId) {
      return;
    }
    let cancelled = false;
    setErrorMessage(null);
    void getTenantDeliverySummary(credentials, tenantId)
      .then((payload) => {
        if (!cancelled) {
          setData(payload);
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setErrorMessage(`Unable to load delivery summary: ${(error as Error).message}`);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [credentials, tenantId]);

  if (!data && !errorMessage) {
    return (
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }).map((_, index) => (
          <Skeleton key={index} className="h-32 rounded-xl" />
        ))}
      </div>
    );
  }

  const summary = data?.summary;

  return (
    <div className="space-y-6">
      {errorMessage ? (
        <div className="rounded-xl border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800">{errorMessage}</div>
      ) : null}

      {summary ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          {[
            { label: "Completed", value: summary.completed_count, icon: <CheckCircle2 className="h-5 w-5 text-emerald-500" /> },
            { label: "In Review", value: summary.in_review_count, icon: <GitPullRequest className="h-5 w-5 text-sky-500" /> },
            { label: "Blocked", value: summary.blocked_count, icon: <AlertCircle className="h-5 w-5 text-amber-500" /> },
            {
              label: "Cycle Time",
              value: `${summary.median_cycle_time_hours ?? "0"}h`,
              sub: "Median completion time",
              icon: <Clock3 className="h-5 w-5 text-violet-500" />
            },
          ].map((item) => (
            <div key={item.label} className="rounded-xl border px-4 py-3">
              <div className="flex items-start justify-between">
                <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">{item.label}</p>
                {item.icon}
              </div>
              <p className="mt-1 text-3xl font-semibold">{item.value}</p>
              {"sub" in item && item.sub ? <p className="mt-0.5 text-xs text-muted-foreground">{item.sub}</p> : null}
            </div>
          ))}
        </div>
      ) : null}

      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="p-6">
          <h2 className="text-base font-semibold">Recent delivery timeline</h2>
        </div>
        <div className="divide-y">
          {data?.timeline.length ? (
            data.timeline.map((item) => (
              <div key={item.run_id} className="px-6 py-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium">{item.issue_key}</p>
                    <p className="text-sm text-muted-foreground">{item.issue_summary || "Untitled work item"}</p>
                  </div>
                  <div className="text-right text-sm text-muted-foreground">
                    <p className="font-medium capitalize text-foreground">{item.status.replace(/_/g, " ")}</p>
                    <p>{item.completed_at ? new Date(item.completed_at).toLocaleString() : "Still in progress"}</p>
                  </div>
                </div>
                {item.pr_url ? (
                  <p className="mt-2 text-sm">
                    <Link href={item.pr_url} target="_blank" className="text-primary underline-offset-2 hover:underline">
                      View pull request
                    </Link>
                  </p>
                ) : null}
              </div>
            ))
          ) : (
            <div className="px-6 pb-6 text-sm text-muted-foreground">No completed work has been recorded for this tenant yet.</div>
          )}
        </div>
      </div>
    </div>
  );
}
