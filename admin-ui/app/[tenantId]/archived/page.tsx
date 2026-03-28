"use client";

import { useParams, useRouter, useSearchParams } from "next/navigation";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export default function ArchivedWorkspacePage() {
  const router = useRouter();
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
      <Card className="w-full max-w-xl border shadow-sm">
        <CardHeader className="space-y-2">
          <CardTitle>Workspace archived</CardTitle>
          <CardDescription>
            {tenantId} has been archived and removed from the active workspace list.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <p className="text-sm text-muted-foreground">
            Archived workspaces stay available for review and unarchive actions, but they no longer open the active
            dashboard.
          </p>
          {purgeDate ? (
            <p className="text-sm text-muted-foreground">This workspace is scheduled for permanent deletion on {purgeDate}.</p>
          ) : null}
          <Button onClick={() => router.push(href)}>{buttonLabel}</Button>
        </CardContent>
      </Card>
    </div>
  );
}
