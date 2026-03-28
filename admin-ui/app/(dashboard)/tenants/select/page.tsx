"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Archive, ChevronRight, Plus, Zap } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { canAccessPlatformAdmin, getDefaultAuthenticatedRoute, getTenantDashboardRoute } from "@/lib/auth-routing";
import { Skeleton } from "@/components/ui/skeleton";
import { listTenants, type TenantRecord } from "@/lib/api";

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

export default function SelectTenantPage() {
  const router = useRouter();
  const { credentials, ready, principal, needsOnboarding } = useAuth();
  const [tenants, setTenants] = useState<TenantRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const activeTenants = tenants.filter((tenant) => tenant.is_enabled);
  const archivedTenants = tenants.filter((tenant) => !tenant.is_enabled);

  async function loadTenants() {
    if (!credentials) return;
    setLoading(true);
    try {
      const payload = await listTenants(credentials);
      setTenants(payload);
      setErrorMessage(null);
    } catch (error) {
      setErrorMessage(`Failed to load tenants: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (ready && principal && !canAccessPlatformAdmin(principal) && !needsOnboarding) {
      router.replace(getDefaultAuthenticatedRoute(principal));
      return;
    }
  }, [needsOnboarding, principal, ready, router]);

  useEffect(() => {
    if (ready && credentials) void loadTenants();
  }, [ready, credentials]);

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-background px-4 py-12">
      {/* Header */}
      <div className="mb-8 flex flex-col items-center gap-3 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-primary shadow-[0_0_24px_rgba(99,102,241,0.3)]">
          <Zap className="h-6 w-6 text-white" />
        </div>
        <div>
          <h1 className="text-2xl font-bold">Select a workspace</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Open an active workspace, or manage archived workspaces below.
          </p>
        </div>
      </div>

      {/* Tenant list */}
      <div className="w-full max-w-md">
        {errorMessage ? (
          <p className="mb-6 rounded-lg border border-destructive/30 bg-destructive/10 px-5 py-4 text-sm leading-relaxed text-destructive break-words">
            {errorMessage}
          </p>
        ) : null}

        {loading ? (
          <div className="overflow-hidden rounded-xl border bg-card shadow-sm">
            <div className="divide-y">
              {[...Array(3)].map((_, i) => (
                <div key={i} className="flex items-center gap-3 px-4 py-4">
                  <Skeleton className="h-9 w-9 rounded-lg flex-shrink-0" />
                  <div className="flex-1 space-y-1.5">
                    <Skeleton className="h-4 w-32" />
                    <Skeleton className="h-3 w-48" />
                  </div>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="overflow-hidden rounded-xl border bg-card shadow-sm">
              <div className="border-b px-4 py-3">
                <p className="text-sm font-semibold">Active workspaces</p>
              </div>
              {activeTenants.length === 0 ? (
                <div className="px-4 py-8 text-center">
                  <p className="text-sm text-muted-foreground">No active workspaces found.</p>
                  <Link
                    href="/tenants/new"
                    className="mt-3 inline-flex items-center gap-1.5 text-sm font-medium text-primary hover:underline"
                  >
                    <Plus className="h-3.5 w-3.5" />
                    Create your first tenant
                  </Link>
                </div>
              ) : (
                <ul className="divide-y">
                  {activeTenants.map((tenant) => (
                    <li key={tenant.tenant_id}>
                      <Link
                        href={getTenantDashboardRoute(tenant.tenant_id)}
                        className="flex items-center gap-3 px-4 py-3.5 transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:bg-muted/50"
                      >
                        <div
                          className={`flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg text-xs font-bold text-white ${tenantAvatarColor(tenant.tenant_id)}`}
                        >
                          {tenantInitials(tenant.name)}
                        </div>
                        <div className="min-w-0 flex-1">
                          <p className="truncate font-medium text-sm">{tenant.name}</p>
                          <p className="truncate text-xs text-muted-foreground">{tenant.tenant_id}</p>
                        </div>
                        <ChevronRight className="h-4 w-4 flex-shrink-0 text-muted-foreground" />
                      </Link>
                    </li>
                  ))}
                  <li>
                    <Link
                      href="/tenants/new"
                      className="flex items-center gap-3 px-4 py-3.5 transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:bg-muted/50"
                    >
                      <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg border-2 border-dashed border-muted-foreground/30 text-muted-foreground">
                        <Plus className="h-4 w-4" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-medium text-muted-foreground">Create new workspace</p>
                        <p className="text-xs text-muted-foreground/70">Add another tenant account</p>
                      </div>
                      <ChevronRight className="h-4 w-4 flex-shrink-0 text-muted-foreground/50" />
                    </Link>
                  </li>
                </ul>
              )}
            </div>

            {archivedTenants.length > 0 ? (
              <div className="overflow-hidden rounded-xl border bg-card shadow-sm">
                <div className="border-b px-4 py-3">
                  <p className="text-sm font-semibold">Archived workspaces</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Archived workspaces no longer open the dashboard. Open settings to review or unarchive them.
                  </p>
                </div>
                <ul className="divide-y">
                  {archivedTenants.map((tenant) => (
                    <li key={tenant.tenant_id}>
                      <Link
                        href={`/tenants/${encodeURIComponent(tenant.tenant_id)}/edit/integrations`}
                        className="flex items-center gap-3 px-4 py-3.5 transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:bg-muted/50"
                      >
                        <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground">
                          <Archive className="h-4 w-4" />
                        </div>
                        <div className="min-w-0 flex-1">
                          <p className="truncate font-medium text-sm">{tenant.name}</p>
                          <p className="truncate text-xs text-muted-foreground">{tenant.tenant_id}</p>
                        </div>
                        <span className="rounded-full border px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                          Archived
                        </span>
                      </Link>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}
