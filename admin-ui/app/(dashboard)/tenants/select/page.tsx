"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Archive, ChevronRight, Plus, Zap } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { canAccessPlatformAdmin, getDefaultAuthenticatedRoute, getTenantDashboardRoute } from "@/lib/auth-routing";
import { readLastWorkspaceTenantIdFromBrowser } from "@/lib/workspace-preference";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { listTenants, type TenantRecord } from "@/lib/api";

const WORKSPACES_PER_PAGE = 6;

function tenantInitials(name: string): string {
  const words = name.trim().split(/\s+/);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  return name.slice(0, 2).toUpperCase();
}

function tenantAvatarColor(id: string): string {
  const colors = [
    "bg-violet-600",
    "bg-blue-600",
    "bg-cyan-600",
    "bg-emerald-600",
    "bg-amber-600",
    "bg-rose-600",
    "bg-pink-600",
    "bg-indigo-600"
  ];
  let hash = 0;
  for (let i = 0; i < id.length; i++) {
    hash = (hash * 31 + id.charCodeAt(i)) & 0xffffffff;
  }
  return colors[Math.abs(hash) % colors.length];
}

function formatPurgeDate(value: string | null | undefined): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export default function SelectTenantPage() {
  const router = useRouter();
  const { credentials, ready, principal, needsOnboarding } = useAuth();
  const [tenants, setTenants] = useState<TenantRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [activePage, setActivePage] = useState(1);
  const [archivedPage, setArchivedPage] = useState(1);
  const activeTenants = tenants.filter((tenant) => tenant.is_enabled);
  const archivedTenants = tenants.filter((tenant) => !tenant.is_enabled);
  const activePageCount = Math.max(1, Math.ceil(activeTenants.length / WORKSPACES_PER_PAGE));
  const archivedPageCount = Math.max(1, Math.ceil(archivedTenants.length / WORKSPACES_PER_PAGE));
  const pagedActiveTenants = useMemo(
    () =>
      activeTenants.slice(
        (activePage - 1) * WORKSPACES_PER_PAGE,
        activePage * WORKSPACES_PER_PAGE,
      ),
    [activePage, activeTenants],
  );
  const pagedArchivedTenants = useMemo(
    () =>
      archivedTenants.slice(
        (archivedPage - 1) * WORKSPACES_PER_PAGE,
        archivedPage * WORKSPACES_PER_PAGE,
      ),
    [archivedPage, archivedTenants],
  );

  async function loadTenants() {
    if (!credentials) return;
    setLoading(true);
    try {
      const payload = await listTenants(credentials);
      setTenants(payload);
      setActivePage(1);
      setArchivedPage(1);
      setErrorMessage(null);
    } catch (error) {
      setErrorMessage(`Failed to load tenants: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (ready && principal && !canAccessPlatformAdmin(principal) && !needsOnboarding) {
      const preferredTenantId = readLastWorkspaceTenantIdFromBrowser();
      router.replace(getDefaultAuthenticatedRoute(principal, { preferredTenantId }));
      return;
    }
  }, [needsOnboarding, principal, ready, router]);

  useEffect(() => {
    if (ready && credentials) void loadTenants();
  }, [ready, credentials]);

  useEffect(() => {
    setActivePage((current) => Math.min(current, activePageCount));
  }, [activePageCount]);

  useEffect(() => {
    setArchivedPage((current) => Math.min(current, archivedPageCount));
  }, [archivedPageCount]);

  return (
    <div className="flex min-h-screen flex-col items-center bg-background px-4 pt-[12vh] pb-12">
      <div className="mb-10 flex flex-col items-center gap-4 text-center">
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-primary shadow-[0_0_20px_rgba(99,102,241,0.25)]">
          <Zap className="h-5 w-5 text-white" />
        </div>
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Switch workspace</h1>
          <p className="mt-1 text-sm text-muted-foreground">Choose a workspace to continue</p>
        </div>
      </div>

      <div className="w-full max-w-lg">
        {errorMessage ? (
          <p className="mb-6 rounded-lg border border-destructive/30 bg-destructive/10 px-5 py-4 text-sm leading-relaxed text-destructive break-words">
            {errorMessage}
          </p>
        ) : null}

        {loading ? (
          <div className="overflow-hidden rounded-2xl border bg-background shadow-sm">
            <div className="divide-y">
              {[...Array(3)].map((_, i) => (
                <div key={i} className="flex items-center gap-4 px-5 py-4">
                  <Skeleton className="h-10 w-10 rounded-xl flex-shrink-0" />
                  <div className="flex-1 space-y-2">
                    <Skeleton className="h-4 w-36" />
                    <Skeleton className="h-3 w-52" />
                  </div>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="space-y-5">
            <div className="overflow-hidden rounded-2xl border bg-background shadow-sm">
              <div className="border-b px-5 py-3.5">
                <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Active workspaces</p>
              </div>
              {activeTenants.length === 0 ? (
                <div className="px-5 py-10 text-center">
                  <p className="text-sm text-muted-foreground">No active workspaces found.</p>
                  <Link
                    href="/tenants/new"
                    className="mt-4 inline-flex items-center gap-1.5 text-sm font-medium text-primary hover:underline"
                  >
                    <Plus className="h-3.5 w-3.5" />
                    Create your first workspace
                  </Link>
                </div>
              ) : (
                <ul className="divide-y">
                  {pagedActiveTenants.map((tenant) => (
                    <li key={tenant.tenant_id}>
                      <Link
                        href={getTenantDashboardRoute(tenant.tenant_id)}
                        className="group flex items-center gap-4 px-5 py-4 transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:bg-muted/50"
                      >
                        <div
                          className={`flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl text-xs font-bold text-white shadow-sm ${tenantAvatarColor(tenant.tenant_id)}`}
                        >
                          {tenantInitials(tenant.name)}
                        </div>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm font-medium">{tenant.name}</p>
                          <p className="truncate text-xs text-muted-foreground">{tenant.tenant_id}</p>
                          {formatPurgeDate(tenant.purge_after_at) ? (
                            <p className="truncate text-xs text-muted-foreground">
                              Deletes on {formatPurgeDate(tenant.purge_after_at)}
                            </p>
                          ) : null}
                        </div>
                        <ChevronRight className="h-4 w-4 flex-shrink-0 text-muted-foreground/40 transition-colors group-hover:text-muted-foreground" />
                      </Link>
                    </li>
                  ))}
                  <li>
                    <Link
                      href="/tenants/new"
                      className="group flex items-center gap-4 px-5 py-4 transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:bg-muted/50"
                    >
                      <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl border-2 border-dashed border-muted-foreground/25 text-muted-foreground">
                        <Plus className="h-4 w-4" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-medium text-muted-foreground">Create new workspace</p>
                        <p className="text-xs text-muted-foreground/60">Add another tenant account</p>
                      </div>
                      <ChevronRight className="h-4 w-4 flex-shrink-0 text-muted-foreground/30 transition-colors group-hover:text-muted-foreground/50" />
                    </Link>
                  </li>
                </ul>
              )}
              {activeTenants.length > WORKSPACES_PER_PAGE ? (
                <div className="flex items-center justify-between border-t px-5 py-3">
                  <span className="text-xs text-muted-foreground">
                    {activeTenants.length} active workspace{activeTenants.length === 1 ? "" : "s"} &middot; Page {activePage} of{" "}
                    {activePageCount}
                  </span>
                  <div className="flex items-center gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-8 text-xs"
                      onClick={() => setActivePage((current) => Math.max(1, current - 1))}
                      disabled={activePage <= 1}
                    >
                      Prev
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-8 text-xs"
                      onClick={() => setActivePage((current) => Math.min(activePageCount, current + 1))}
                      disabled={activePage >= activePageCount}
                    >
                      Next
                    </Button>
                  </div>
                </div>
              ) : null}
            </div>

            {archivedTenants.length > 0 ? (
              <div className="overflow-hidden rounded-2xl border bg-background shadow-sm">
                <div className="border-b px-5 py-3.5">
                  <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Archived workspaces</p>
                  <p className="mt-1.5 text-xs text-muted-foreground">
                    Archived workspaces are read-only. Open settings to review or restore them.
                  </p>
                </div>
                <ul className="divide-y">
                  {pagedArchivedTenants.map((tenant) => (
                    <li key={tenant.tenant_id}>
                      <Link
                        href={`/${encodeURIComponent(tenant.tenant_id)}/settings/config`}
                        className="group flex items-center gap-4 px-5 py-4 transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:bg-muted/50"
                      >
                        <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl bg-muted text-muted-foreground">
                          <Archive className="h-4 w-4" />
                        </div>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm font-medium">{tenant.name}</p>
                          <p className="truncate text-xs text-muted-foreground">{tenant.tenant_id}</p>
                        </div>
                        <span className="rounded-full border px-2.5 py-0.5 text-[11px] font-medium text-muted-foreground">
                          Archived
                        </span>
                      </Link>
                    </li>
                  ))}
                </ul>
                {archivedTenants.length > WORKSPACES_PER_PAGE ? (
                  <div className="flex items-center justify-between border-t px-5 py-3">
                    <span className="text-xs text-muted-foreground">
                      {archivedTenants.length} archived workspace{archivedTenants.length === 1 ? "" : "s"} &middot; Page{" "}
                      {archivedPage} of {archivedPageCount}
                    </span>
                    <div className="flex items-center gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-8 text-xs"
                        onClick={() => setArchivedPage((current) => Math.max(1, current - 1))}
                        disabled={archivedPage <= 1}
                      >
                        Prev
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-8 text-xs"
                        onClick={() => setArchivedPage((current) => Math.min(archivedPageCount, current + 1))}
                        disabled={archivedPage >= archivedPageCount}
                      >
                        Next
                      </Button>
                    </div>
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}
