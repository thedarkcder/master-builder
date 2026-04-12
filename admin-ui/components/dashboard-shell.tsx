"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import type { ComponentType } from "react";
import {
  Activity,
  BarChart3,
  Building2,
  Cpu,
  FolderKanban,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Menu,
  Settings2,
  SwitchCamera,
  User,
  Users,
  X,
  Zap
} from "lucide-react";

import { ThemeToggle } from "@/components/ui/theme-toggle";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/components/auth-provider";
import { getRun, type TenantRecord, getTenant, listProjects, type ProjectRecord } from "@/lib/api";
import {
  canAccessPlatformAdmin,
  canAccessTechnicalSurface,
  canManageTeam,
  getMembershipForTenant,
  getTenantWorkspaceRoute,
} from "@/lib/auth-routing";
import { buildProjectSectionPath, resolveRunRouteContext } from "@/lib/dashboard-paths";
import { persistLastWorkspaceTenantId } from "@/lib/workspace-preference";
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
  /** Pathname match for active state when `href` includes a `#fragment`. */
  activePathname?: string;
};

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

type RunRouteCtx = ReturnType<typeof resolveRunRouteContext>;

type DashboardNavPanelProps = {
  decodedTenantId: string | null;
  tenantDisplayName: string;
  tenantBaseRoute: string | null;
  navItems: NavItem[];
  tenantProjects: ProjectRecord[];
  pathname: string;
  runContext: RunRouteCtx;
  projectContextId: string | null;
  router: ReturnType<typeof useRouter>;
  logout: () => void;
  onNavigate?: () => void;
};

/** Shared nav body for desktop sidebar and mobile drawer */
function DashboardNavPanel({
  decodedTenantId,
  tenantDisplayName,
  tenantBaseRoute,
  navItems,
  tenantProjects,
  pathname,
  runContext,
  projectContextId,
  router,
  logout,
  onNavigate
}: DashboardNavPanelProps) {
  const close = () => onNavigate?.();
  const projectMenu = (
    <SidebarMenu>
      <SidebarMenuItem key={`projects-overview-${decodedTenantId}`}>
        <SidebarMenuButton asChild isActive={tenantBaseRoute != null && pathname === `${tenantBaseRoute}/projects`}>
          <Link href={`${tenantBaseRoute}/projects`} onClick={close}>
            <FolderKanban className="h-4 w-4 flex-shrink-0" />
            All projects
          </Link>
        </SidebarMenuButton>
      </SidebarMenuItem>
      {tenantProjects.map((project) => {
        const projectHref = buildProjectSectionPath(decodedTenantId ?? project.tenant_id, project.project_id, "runs");
        const active =
          pathname === buildProjectSectionPath(decodedTenantId ?? project.tenant_id, project.project_id) ||
          pathname.startsWith(`${buildProjectSectionPath(decodedTenantId ?? project.tenant_id, project.project_id)}/`) ||
          (Boolean(runContext.runId) && project.project_id === projectContextId);
        return (
          <SidebarMenuItem key={project.project_id}>
            <SidebarMenuButton asChild isActive={active}>
              <Link href={projectHref} onClick={close}>
                <FolderKanban className="h-4 w-4 flex-shrink-0" />
                {project.name}
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        );
      })}
    </SidebarMenu>
  );

  return (
    <>
      <SidebarHeader className="shrink-0">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary shadow-[0_0_12px_rgba(99,102,241,0.4)]">
              <Zap className="h-4 w-4 text-white" />
            </div>
            <div>
              <p className="text-sm font-semibold text-sidebar-foreground leading-none">Master Builder</p>
              <p className="text-[10px] text-sidebar-muted-foreground mt-0.5">AI Orchestrator</p>
            </div>
          </div>
          {onNavigate ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-10 w-10 shrink-0 p-0 text-sidebar-foreground hover:bg-sidebar-accent md:hidden"
              onClick={close}
              aria-label="Close menu"
            >
              <X className="h-5 w-5" />
            </Button>
          ) : null}
        </div>
      </SidebarHeader>

      <SidebarContent className="flex min-h-0 flex-1 flex-col overflow-hidden">
        {decodedTenantId ? (
          <div className="flex min-h-0 flex-1 flex-col">
            <div className="mb-3 flex items-center gap-2 rounded-md px-3 py-2 bg-sidebar-accent/40">
              <div
                className={`flex h-6 w-6 flex-shrink-0 items-center justify-center rounded text-[10px] font-bold text-white ${tenantAvatarColor(decodedTenantId)}`}
              >
                {tenantInitials(tenantDisplayName)}
              </div>
              <span className="truncate text-xs font-medium text-sidebar-foreground">{tenantDisplayName}</span>
            </div>

            <SidebarMenu className="shrink-0">
              {navItems.map((item) => {
                const active = item.activePathname
                  ? pathname === item.activePathname
                  : item.matchPrefix
                    ? pathname.startsWith(item.matchPrefix) &&
                      !(item.label === "Pipeline" && Boolean(runContext.runId) && Boolean(projectContextId))
                    : pathname === item.href.split("#")[0];
                return (
                  <SidebarMenuItem key={item.href}>
                    <SidebarMenuButton asChild isActive={active}>
                      <Link href={item.href} onClick={close}>
                        <item.icon className="h-4 w-4 flex-shrink-0" />
                        {item.label}
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>

            <div className="mt-4 flex min-h-0 flex-1 flex-col">
              <SidebarMenuLabel className="px-0">Projects</SidebarMenuLabel>
              <div className="min-h-0 flex-1 overflow-y-auto pr-1">{projectMenu}</div>
            </div>
          </div>
        ) : (
          <div className="flex min-h-0 flex-1 flex-col">
            <SidebarMenu className="shrink-0">
              <SidebarMenuLabel>Global</SidebarMenuLabel>
              {navItems.map((item) => {
                const active = item.activePathname
                  ? pathname === item.activePathname
                  : item.matchPrefix
                    ? pathname.startsWith(item.matchPrefix)
                    : pathname === item.href.split("#")[0] ||
                      pathname.startsWith(`${item.href.split("#")[0]}/`);
                return (
                  <SidebarMenuItem key={item.href}>
                    <SidebarMenuButton asChild isActive={active}>
                      <Link href={item.href} onClick={close}>
                        <item.icon className="h-4 w-4 flex-shrink-0" />
                        {item.label}
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          </div>
        )}
      </SidebarContent>

      <SidebarFooter className="mt-auto shrink-0 bg-sidebar">
        <div className="flex flex-wrap items-center gap-1">
          <ThemeToggle />
          <button
            type="button"
            className="flex min-h-10 flex-1 items-center gap-2 rounded-md px-2 py-2 text-xs text-sidebar-foreground/70 transition-colors hover:bg-sidebar-accent/50 hover:text-sidebar-foreground"
            onClick={() => {
              close();
              router.push("/tenants/select");
            }}
          >
            <SwitchCamera className="h-3.5 w-3.5 flex-shrink-0" />
            Switch Tenant
          </button>
          <button
            type="button"
            className="flex min-h-10 items-center gap-2 rounded-md px-2 py-2 text-xs text-sidebar-foreground/70 transition-colors hover:bg-sidebar-accent/50 hover:text-sidebar-foreground"
            onClick={() => {
              close();
              void logout();
            }}
          >
            <LogOut className="h-3.5 w-3.5 flex-shrink-0" />
            Logout
          </button>
        </div>
      </SidebarFooter>
    </>
  );
}

export function DashboardShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { credentials, ready, logout, needsOnboarding, principal, principalReady } = useAuth();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [tenant, setTenant] = useState<TenantRecord | null>(null);
  const [tenantProjects, setTenantProjects] = useState<ProjectRecord[]>([]);
  const tenantMatch = pathname.match(
    /^\/(?!tenants(?:\/|$)|runs(?:\/|$)|platform(?:\/|$)|login(?:\/|$)|register(?:\/|$)|invite(?:\/|$)|get-started(?:\/|$)|forgot-password(?:\/|$)|reset-password(?:\/|$)|api(?:\/|$))([^/]+)\//,
  );
  const projectMatch = pathname.match(
    /^\/(?!tenants(?:\/|$)|runs(?:\/|$)|platform(?:\/|$)|login(?:\/|$)|register(?:\/|$)|invite(?:\/|$)|get-started(?:\/|$)|forgot-password(?:\/|$)|reset-password(?:\/|$)|api(?:\/|$))([^/]+)\/projects\/([^/]+)(?:\/|$)/,
  );
  const runContext = resolveRunRouteContext(pathname);
  const isWizardRoute =
    pathname.startsWith("/tenants/new") ||
    /^\/(?!tenants(?:\/|$)|runs(?:\/|$)|platform(?:\/|$)|login(?:\/|$)|register(?:\/|$)|invite(?:\/|$)|get-started(?:\/|$)|forgot-password(?:\/|$)|reset-password(?:\/|$)|api(?:\/|$))[^/]+\/projects\/new(\/|$)/.test(pathname);
  const [runTenantId, setRunTenantId] = useState<string | null>(null);
  const tenantId = tenantMatch ? tenantMatch[1] : runTenantId;
  const projectContextId = projectMatch?.[2] ? decodeURIComponent(projectMatch[2]) : runContext.projectId || null;

  useEffect(() => {
    setMobileNavOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (ready && !credentials) {
      router.replace("/login");
    }
  }, [credentials, ready, router]);

  useEffect(() => {
    if (ready && credentials && principalReady && !principal) {
      void logout();
      router.replace("/login");
    }
  }, [credentials, logout, principal, principalReady, ready, router]);

  useEffect(() => {
    const onboardingAllowed =
      pathname === "/get-started" ||
      pathname.startsWith("/tenants/new") ||
      pathname.startsWith("/platform/agent-runtimes") ||
      /^\/(?!tenants(?:\/|$)|runs(?:\/|$)|platform(?:\/|$)|login(?:\/|$)|register(?:\/|$)|invite(?:\/|$)|get-started(?:\/|$)|forgot-password(?:\/|$)|reset-password(?:\/|$)|api(?:\/|$))[^/]+\/settings(\/|$)/.test(pathname);
    if (ready && credentials && needsOnboarding && !onboardingAllowed) {
      router.replace("/get-started");
    }
  }, [credentials, needsOnboarding, pathname, ready, router]);

  useEffect(() => {
    if (!credentials || !runContext.runId) {
      setRunTenantId(null);
      return;
    }
    let cancelled = false;
    void getRun(credentials, runContext.runId)
      .then((run) => {
        if (!cancelled) {
          setRunTenantId(run.tenant_id);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setRunTenantId(null);
        }
      });
    return () => { cancelled = true; };
  }, [credentials, runContext.runId]);

  useEffect(() => {
    if (!credentials || !tenantId || isWizardRoute) {
      setTenant(null);
      return;
    }
    let cancelled = false;
    void getTenant(credentials, decodeURIComponent(tenantId))
      .then((t) => { if (!cancelled) setTenant(t); })
      .catch(() => { if (!cancelled) setTenant(null); });
    return () => { cancelled = true; };
  }, [credentials, isWizardRoute, tenantId]);

  const decodedTenantIdForPersist = tenantId ? decodeURIComponent(tenantId) : null;
  useEffect(() => {
    if (!decodedTenantIdForPersist || isWizardRoute) {
      return;
    }
    persistLastWorkspaceTenantId(decodedTenantIdForPersist);
  }, [decodedTenantIdForPersist, isWizardRoute]);

  useEffect(() => {
    if (!credentials || !tenantId || isWizardRoute) {
      setTenantProjects([]);
      return;
    }
    let cancelled = false;
    const decodedTenantId = decodeURIComponent(tenantId);
    void listProjects(credentials, decodedTenantId)
      .then((projects) => {
        if (cancelled) {
          return;
        }
        const active = projects
          .filter((project) => !project.is_archived)
          .sort((a, b) => a.name.localeCompare(b.name));
        setTenantProjects(active);
      })
      .catch(() => {
        if (!cancelled) {
          setTenantProjects([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [credentials, isWizardRoute, tenantId]);

  if (!ready) {
    return <main className="p-8 text-sm text-muted-foreground">Loading session...</main>;
  }

  if (!credentials) {
    return <main className="p-8 text-sm text-muted-foreground">Redirecting to login...</main>;
  }

  if (
    needsOnboarding &&
    pathname !== "/get-started" &&
    !pathname.startsWith("/tenants/new") &&
    !pathname.startsWith("/platform/agent-runtimes") &&
    !/^\/(?!tenants(?:\/|$)|runs(?:\/|$)|platform(?:\/|$)|login(?:\/|$)|register(?:\/|$)|invite(?:\/|$)|get-started(?:\/|$)|forgot-password(?:\/|$)|reset-password(?:\/|$)|api(?:\/|$))[^/]+\/settings(\/|$)/.test(pathname)
  ) {
    return <main className="p-8 text-sm text-muted-foreground">Redirecting to onboarding...</main>;
  }

  if (isWizardRoute) {
    return (
      <div className="min-h-screen bg-background px-4 py-6 sm:px-6 sm:py-8">
        <div className="mx-auto w-full max-w-[1320px]">{children}</div>
      </div>
    );
  }

  if (pathname === "/tenants/select") {
    return <div className="min-h-screen bg-background">{children}</div>;
  }

  const decodedTenantId = tenantId ? decodeURIComponent(tenantId) : null;
  const tenantBaseRoute = decodedTenantId ? getTenantWorkspaceRoute(decodedTenantId) : null;
  const isPlatformSuperAdmin = canAccessPlatformAdmin(principal);
  const globalNavItems: NavItem[] = [
    { href: "/platform/dashboard", label: "Dashboard", icon: LayoutDashboard },
    { href: "/platform/status", label: "Status", icon: Activity },
    ...(isPlatformSuperAdmin ? [{ href: "/platform/agent-runtimes", label: "Agent runtimes", icon: Cpu }] : []),
    { href: "/tenants/select", label: "Tenants", icon: Building2 },
    { href: "/platform/secrets", label: "Secrets", icon: KeyRound }
  ];
  const tenantMembership = decodedTenantId ? getMembershipForTenant(principal, decodedTenantId) : null;
  const canManageWorkspaceTeam = decodedTenantId ? canManageTeam(principal, decodedTenantId) : false;
  const analyticsHref =
    decodedTenantId && !canAccessTechnicalSurface(principal, decodedTenantId)
      ? `${tenantBaseRoute}/analytics/business`
      : decodedTenantId
        ? `${tenantBaseRoute}/analytics/token-overview`
        : null;
  const showSecretsNav = !decodedTenantId || canAccessPlatformAdmin(principal) || canAccessTechnicalSurface(principal, decodedTenantId);

  const tenantNavItems: NavItem[] = decodedTenantId
    ? [
        {
          href: `${tenantBaseRoute}/dashboard`,
          label: "Home",
          icon: LayoutDashboard,
          matchPrefix: `${tenantBaseRoute}/dashboard`
        },
        {
          href: `${tenantBaseRoute}/runs`,
          label: "Pipeline",
          icon: Activity,
          matchPrefix: `${tenantBaseRoute}/runs`
        },
        {
          href: analyticsHref ?? `${tenantBaseRoute}/analytics/token-overview`,
          label: "Analytics",
          icon: BarChart3,
          matchPrefix: `${tenantBaseRoute}/analytics`
        },
        ...(canManageWorkspaceTeam
          ? [
              {
                href: `${tenantBaseRoute}/team/members`,
                label: "Team",
                icon: Users,
                matchPrefix: `${tenantBaseRoute}/team`
              }
            ]
          : []),
        {
          href: `${tenantBaseRoute}/profile`,
          label: "Profile",
          icon: User,
          matchPrefix: `${tenantBaseRoute}/profile`
        },
        {
          href: `${tenantBaseRoute}/settings/integrations`,
          label: "Settings",
          icon: Settings2,
          matchPrefix: `${tenantBaseRoute}/settings`
        },
        ...(showSecretsNav
          ? [
              {
                href: `${tenantBaseRoute}/secrets`,
                label: "Secrets",
                icon: KeyRound,
                matchPrefix: `${tenantBaseRoute}/secrets`
              }
            ]
          : [])
      ]
    : [];

  const navItems = decodedTenantId ? tenantNavItems : globalNavItems;
  const tenantDisplayName = tenant?.name ?? decodedTenantId ?? "";

  const navPanelProps: DashboardNavPanelProps = {
    decodedTenantId,
    tenantDisplayName,
    tenantBaseRoute,
    navItems,
    tenantProjects,
    pathname,
    runContext,
    projectContextId,
    router,
    logout
  };

  return (
    <SidebarProvider className="flex min-h-screen w-full flex-col bg-background md:flex-row">
      {/* Mobile drawer */}
      {mobileNavOpen ? (
        <div className="fixed inset-0 z-50 md:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
          <button
            type="button"
            className="absolute inset-0 bg-black/60"
            aria-label="Close menu"
            onClick={() => setMobileNavOpen(false)}
          />
          <aside className="absolute left-0 top-0 flex h-full w-[min(280px,88vw)] flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground shadow-xl">
            <DashboardNavPanel {...navPanelProps} onNavigate={() => setMobileNavOpen(false)} />
          </aside>
        </div>
      ) : null}

      <Sidebar>
        <DashboardNavPanel {...navPanelProps} />
      </Sidebar>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-40 flex items-center gap-3 border-b bg-background/95 px-3 py-2 backdrop-blur supports-[backdrop-filter]:bg-background/80 md:hidden">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-10 w-10 shrink-0 p-0"
            onClick={() => setMobileNavOpen(true)}
            aria-label="Open menu"
          >
            <Menu className="h-5 w-5" />
          </Button>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold">Master Builder</p>
            {decodedTenantId ? (
              <p className="truncate text-xs text-muted-foreground">{tenantDisplayName}</p>
            ) : (
              <p className="truncate text-xs text-muted-foreground">Admin</p>
            )}
          </div>
          <ThemeToggle className="h-10 w-10 shrink-0" />
        </header>

        <SidebarInset className="min-h-0 flex-1 overflow-auto">
          <div className="mx-auto flex w-full max-w-none flex-col gap-6 px-4 py-4 sm:px-6 sm:py-6">{children}</div>
        </SidebarInset>
      </div>
    </SidebarProvider>
  );
}
