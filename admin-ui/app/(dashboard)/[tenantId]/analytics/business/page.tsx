"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { AlertCircle, CheckCircle2, Clock3, GitPullRequest } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{errorMessage}</div>
      ) : null}

      {summary ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm text-muted-foreground">Completed</CardTitle>
            </CardHeader>
            <CardContent className="flex items-end justify-between">
              <span className="text-3xl font-semibold">{summary.completed_count}</span>
              <CheckCircle2 className="h-5 w-5 text-emerald-500" />
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm text-muted-foreground">In Review</CardTitle>
            </CardHeader>
            <CardContent className="flex items-end justify-between">
              <span className="text-3xl font-semibold">{summary.in_review_count}</span>
              <GitPullRequest className="h-5 w-5 text-sky-500" />
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm text-muted-foreground">Blocked</CardTitle>
            </CardHeader>
            <CardContent className="flex items-end justify-between">
              <span className="text-3xl font-semibold">{summary.blocked_count}</span>
              <AlertCircle className="h-5 w-5 text-amber-500" />
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm text-muted-foreground">Cycle Time</CardTitle>
            </CardHeader>
            <CardContent className="flex items-end justify-between">
              <div>
                <p className="text-3xl font-semibold">{summary.median_cycle_time_hours ?? "0"}h</p>
                <p className="text-xs text-muted-foreground">Median completion time</p>
              </div>
              <Clock3 className="h-5 w-5 text-violet-500" />
            </CardContent>
          </Card>
        </div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Recent delivery timeline</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {data?.timeline.length ? (
            data.timeline.map((item) => (
              <div key={item.run_id} className="rounded-xl border px-4 py-3">
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
            <p className="text-sm text-muted-foreground">No completed work has been recorded for this tenant yet.</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
