"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import type { ComponentType } from "react";
import {
  Activity,
  BarChart3,
  Building2,
  FolderKanban,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Settings2,
  SwitchCamera,
  Zap
} from "lucide-react";

import { ThemeToggle } from "@/components/ui/theme-toggle";
import { useAuth } from "@/components/auth-provider";
import { getRun, type TenantRecord, getTenant } from "@/lib/api";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuLabel,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider
} from "@/components/ui/sidebar";

type NavItem = {
  href: string;
  label: string;
  icon: ComponentType<{ className?: string }>;
  matchPrefix?: string;
};

const globalNavItems: NavItem[] = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { href: "/tenants/select", label: "Tenants", icon: Building2 },
  { href: "/secrets", label: "Secrets", icon: KeyRound }
];

function tenantInitials(name: string): string {
  const words = name.trim().split(/\s+/);
  if (words.length >= 2) {
    return (words[0][0] + words[1][0]).toUpperCase();
  }
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

export function DashboardShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { credentials, ready, logout } = useAuth();
  const [tenant, setTenant] = useState<TenantRecord | null>(null);
  const tenantMatch = pathname.match(/^\/tenants\/([^/]+)\//);
  const runMatch = pathname.match(/^\/runs\/([^/]+)$/);
  const [runTenantId, setRunTenantId] = useState<string | null>(null);
  const tenantId = tenantMatch ? tenantMatch[1] : runTenantId;

  useEffect(() => {
    if (ready && !credentials) {
      logout();
      router.replace("/login");
    }
  }, [credentials, logout, ready, router]);

  useEffect(() => {
    if (!credentials || !runMatch) {
      setRunTenantId(null);
      return;
    }
    let cancelled = false;
    const runId = decodeURIComponent(runMatch[1]);
    void getRun(credentials, runId)
      .then((run) => {
        if (!cancelled) setRunTenantId(run.tenant_id);
      })
      .catch(() => {
        if (!cancelled) setRunTenantId(null);
      });
    return () => { cancelled = true; };
  }, [credentials, runMatch]);

  useEffect(() => {
    if (!credentials || !tenantId) {
      setTenant(null);
      return;
    }
    let cancelled = false;
    void getTenant(credentials, decodeURIComponent(tenantId))
      .then((t) => { if (!cancelled) setTenant(t); })
      .catch(() => { if (!cancelled) setTenant(null); });
    return () => { cancelled = true; };
  }, [credentials, tenantId]);

  if (!ready) {
    return <main className="p-8 text-sm text-muted-foreground">Loading session...</main>;
  }

  if (!credentials) {
    return <main className="p-8 text-sm text-muted-foreground">Redirecting to login...</main>;
  }

  const isWizardRoute =
    pathname.startsWith("/tenants/new") ||
    /^\/tenants\/[^/]+\/projects\/new(\/|$)/.test(pathname);

  if (isWizardRoute) {
    return (
      <div className="min-h-screen bg-background px-4 py-10">
        <div className="mx-auto w-full max-w-3xl">{children}</div>
      </div>
    );
  }

  if (pathname === "/tenants/select") {
    return <div className="min-h-screen bg-background">{children}</div>;
  }

  const decodedTenantId = tenantId ? decodeURIComponent(tenantId) : null;

  const tenantNavItems: NavItem[] = decodedTenantId
    ? [
        {
          href: `/tenants/${decodedTenantId}/dashboard`,
          label: "Home",
          icon: LayoutDashboard,
          matchPrefix: `/tenants/${decodedTenantId}/dashboard`
        },
        {
          href: `/tenants/${decodedTenantId}/runs`,
          label: "Pipeline",
          icon: Activity,
          matchPrefix: `/tenants/${decodedTenantId}/runs`
        },
        {
          href: `/tenants/${decodedTenantId}/projects`,
          label: "Projects",
          icon: FolderKanban,
          matchPrefix: `/tenants/${decodedTenantId}/projects`
        },
        {
          href: `/tenants/${decodedTenantId}/analytics/token-overview`,
          label: "Analytics",
          icon: BarChart3,
          matchPrefix: `/tenants/${decodedTenantId}/analytics`
        },
        {
          href: `/tenants/${decodedTenantId}/edit/integrations`,
          label: "Settings",
          icon: Settings2,
          matchPrefix: `/tenants/${decodedTenantId}/edit`
        },
        {
          href: `/tenants/${decodedTenantId}/secrets`,
          label: "Secrets",
          icon: KeyRound,
          matchPrefix: `/tenants/${decodedTenantId}/secrets`
        }
      ]
    : [];

  const navItems = decodedTenantId ? tenantNavItems : globalNavItems;
  const tenantDisplayName = tenant?.name ?? decodedTenantId ?? "";

  return (
    <SidebarProvider>
      <Sidebar>
        {/* Branding */}
        <SidebarHeader>
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary shadow-[0_0_12px_rgba(99,102,241,0.4)]">
              <Zap className="h-4 w-4 text-white" />
            </div>
            <div>
              <p className="text-sm font-semibold text-sidebar-foreground leading-none">Master Builder</p>
              <p className="text-[10px] text-sidebar-muted-foreground mt-0.5">AI Orchestrator</p>
            </div>
          </div>
        </SidebarHeader>

        <SidebarContent>
          {decodedTenantId ? (
            <div className="space-y-1">
              {/* Tenant identity */}
              <div className="mb-3 flex items-center gap-2 rounded-md px-3 py-2 bg-sidebar-accent/40">
                <div
                  className={`flex h-6 w-6 flex-shrink-0 items-center justify-center rounded text-[10px] font-bold text-white ${tenantAvatarColor(decodedTenantId)}`}
                >
                  {tenantInitials(tenantDisplayName)}
                </div>
                <span className="truncate text-xs font-medium text-sidebar-foreground">
                  {tenantDisplayName}
                </span>
              </div>

              <SidebarMenu>
                {navItems.map((item) => {
                  const active = item.matchPrefix
                    ? pathname.startsWith(item.matchPrefix)
                    : pathname === item.href;
                  return (
                    <SidebarMenuItem key={item.href}>
                      <SidebarMenuButton asChild isActive={active}>
                        <Link href={item.href}>
                          <item.icon className="h-4 w-4 flex-shrink-0" />
                          {item.label}
                        </Link>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>

            </div>
          ) : (
            <SidebarMenu>
              <SidebarMenuLabel>Global</SidebarMenuLabel>
              {navItems.map((item) => {
                const active = item.matchPrefix
                  ? pathname.startsWith(item.matchPrefix)
                  : pathname === item.href || pathname.startsWith(`${item.href}/`);
                return (
                  <SidebarMenuItem key={item.href}>
                    <SidebarMenuButton asChild isActive={active}>
                      <Link href={item.href}>
                        <item.icon className="h-4 w-4 flex-shrink-0" />
                        {item.label}
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          )}
        </SidebarContent>

        <SidebarFooter>
          <div className="flex items-center gap-1">
            <ThemeToggle />
            <button
              className="flex flex-1 items-center gap-2 rounded-md px-2 py-1.5 text-xs text-sidebar-foreground/70 transition-colors hover:bg-sidebar-accent/50 hover:text-sidebar-foreground"
              onClick={() => router.push("/tenants/select")}
            >
              <SwitchCamera className="h-3.5 w-3.5 flex-shrink-0" />
              Switch Tenant
            </button>
            <button
              className="flex items-center gap-2 rounded-md px-2 py-1.5 text-xs text-sidebar-foreground/70 transition-colors hover:bg-sidebar-accent/50 hover:text-sidebar-foreground"
              onClick={() => { logout(); router.push("/login"); }}
            >
              <LogOut className="h-3.5 w-3.5 flex-shrink-0" />
              Logout
            </button>
          </div>
        </SidebarFooter>
      </Sidebar>

      <SidebarInset>
        <div className="flex h-full flex-col">
          <div className="mx-auto flex w-full max-w-none flex-col gap-6 px-6 py-6">{children}</div>
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}
