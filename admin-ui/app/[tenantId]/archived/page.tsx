"use client";

import { Suspense } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";

import { Button } from "@/components/ui/button";


export default function ArchivedWorkspacePage() {
  return (
    <Suspense fallback={<ArchivedWorkspaceFallback />}>
      <ArchivedWorkspacePageInner />
    </Suspense>
  );
}

function ArchivedWorkspaceFallback() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-6 py-12">
      <p className="text-sm text-muted-foreground">Loading…</p>
    </div>
  );
}

function ArchivedWorkspacePageInner() {
  const params = useParams<{ tenantId: string }>();
  const searchParams = useSearchParams();

  const tenantId = params.tenantId;
  const destination = searchParams.get("destination");
  const next = searchParams.get("next");
  const purgeAfter = searchParams.get("purge_after");
  const href = next && next.startsWith("/") ? next : "/tenants/select";
  const buttonLabel =
    destination === "setup" ? "Open archived workspace settings" : "View archived workspaces";
  const purgeDate = purgeAfter
    ? new Date(purgeAfter).toLocaleDateString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
      })
    : null;

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-6 py-12">
      <div className="w-full max-w-xl overflow-hidden rounded-2xl border bg-background shadow-sm">
        <div className="space-y-2 p-6 pb-3">
          <h2 className="text-base font-semibold">Workspace archived</h2>
          <p className="text-sm text-muted-foreground">
            {tenantId} has been archived and removed from the active workspace list.
          </p>
        </div>
        <div className="space-y-6 p-6 pt-0">
          <p className="text-sm text-muted-foreground">
            Archived workspaces stay available for review and unarchive actions, but they no longer open the active
            dashboard.
          </p>
          {purgeDate ? (
            <p className="text-sm text-muted-foreground">This workspace is scheduled for permanent deletion on {purgeDate}.</p>
          ) : null}
          <Button asChild>
            <Link href={href}>{buttonLabel}</Link>
          </Button>
        </div>
      </div>
    </div>
  );
}
