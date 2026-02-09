"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import type { ComponentType } from "react";
import {
  Activity,
  Bell,
  Building2,
  FolderKanban,
  KeyRound,
  LayoutDashboard,
  LogOut,
  PlugZap,
  Settings2,
  ShieldCheck,
  MessageSquare
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/components/auth-provider";
import { listProjects, type ProjectRecord } from "@/lib/api";
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
  { href: "/tenants/select", label: "Select Tenant", icon: Building2 },
  { href: "/secrets", label: "Secrets", icon: KeyRound }
];

export function DashboardShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { credentials, ready, logout } = useAuth();
  const [tenantProjects, setTenantProjects] = useState<ProjectRecord[]>([]);
  const tenantMatch = pathname.match(/^\/tenants\/([^/]+)\//);
  const tenantId = tenantMatch ? tenantMatch[1] : null;

  useEffect(() => {
    if (ready && !credentials) {
      logout();
      router.replace("/login");
    }
  }, [credentials, logout, ready, router]);

  useEffect(() => {
    if (!credentials || !tenantId) {
      setTenantProjects([]);
      return;
    }
    let cancelled = false;
    void listProjects(credentials, decodeURIComponent(tenantId))
      .then((projects) => {
        if (!cancelled) {
          setTenantProjects(projects);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setTenantProjects([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [credentials, tenantId]);

  if (!ready) {
    return <main className="p-8 text-sm text-muted-foreground">Loading session...</main>;
  }

  if (!credentials) {
    return <main className="p-8 text-sm text-muted-foreground">Redirecting to login...</main>;
  }

  const isWizardRoute =
    pathname.startsWith("/tenants/new") || /^\/tenants\/[^/]+\/projects\/new(\/|$)/.test(pathname);

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

  const tenantNavItems: NavItem[] = tenantId
      ? [
        {
          href: `/tenants/${tenantId}/edit/integrations`,
          label: "Integrations",
          icon: PlugZap,
          matchPrefix: `/tenants/${tenantId}/edit/integrations`
        },
        {
          href: `/tenants/${tenantId}/edit/jira`,
          label: "Jira",
          icon: PlugZap,
          matchPrefix: `/tenants/${tenantId}/edit/jira`
        },
        {
          href: `/tenants/${tenantId}/edit/github`,
          label: "GitHub",
          icon: ShieldCheck,
          matchPrefix: `/tenants/${tenantId}/edit/github`
        },
        {
          href: `/tenants/${tenantId}/edit/discord`,
          label: "Discord",
          icon: MessageSquare,
          matchPrefix: `/tenants/${tenantId}/edit/discord`
        },
        {
          href: `/tenants/${tenantId}/secrets`,
          label: "Secrets",
          icon: KeyRound,
          matchPrefix: `/tenants/${tenantId}/secrets`
        },
        {
          href: `/tenants/${tenantId}/projects`,
          label: "Projects",
          icon: FolderKanban,
          matchPrefix: `/tenants/${tenantId}/projects`
        },
        {
          href: `/tenants/${tenantId}/edit/health`,
          label: "Health",
          icon: Activity,
          matchPrefix: `/tenants/${tenantId}/edit/health`
        },
        {
          href: `/tenants/${tenantId}/edit/config`,
          label: "Configuration",
          icon: Settings2,
          matchPrefix: `/tenants/${tenantId}/edit/config`
        },
        {
          href: `/tenants/${tenantId}/edit/notifications`,
          label: "Notifications",
          icon: Bell,
          matchPrefix: `/tenants/${tenantId}/edit/notifications`
        }
      ]
    : [];
  const navItems = tenantId ? tenantNavItems : globalNavItems;
  const integrationItems = tenantId
    ? navItems.filter((item) =>
        ["/edit/integrations", "/edit/jira", "/edit/github", "/edit/discord"].some((suffix) =>
          item.href.endsWith(suffix)
        )
      )
    : [];
  const tenantWorkspaceItems = tenantId
    ? navItems.filter(
        (item) =>
          item.href !== `/tenants/${tenantId}/projects` &&
          !integrationItems.some((integrationsItem) => integrationsItem.href === item.href)
      )
    : [];

  return (
    <SidebarProvider>
      <Sidebar>
        <SidebarHeader>
          <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground">Master Builder</p>
        </SidebarHeader>
        <SidebarContent>
          {!tenantId ? (
            <SidebarMenu>
              {navItems.map((item) => {
                const active = item.matchPrefix
                  ? pathname.startsWith(item.matchPrefix)
                  : pathname === item.href || pathname.startsWith(`${item.href}/`);
                return (
                  <SidebarMenuItem key={item.href}>
                    <SidebarMenuButton asChild isActive={active}>
                      <Link href={item.href}>
                        <item.icon className="h-4 w-4" />
                        {item.label}
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          ) : (
            <div className="space-y-4">
              <SidebarMenu>
                <SidebarMenuLabel>Tenant Workspace</SidebarMenuLabel>
                {tenantWorkspaceItems.map((item) => {
                  const active = item.matchPrefix
                    ? pathname.startsWith(item.matchPrefix)
                    : pathname === item.href || pathname.startsWith(`${item.href}/`);
                  return (
                    <SidebarMenuItem key={item.href}>
                      <SidebarMenuButton asChild isActive={active}>
                        <Link href={item.href}>
                          <item.icon className="h-4 w-4" />
                          {item.label}
                        </Link>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  );
                })}
                {tenantId ? (
                  <SidebarMenuItem>
                    <SidebarMenuButton
                      asChild
                      isActive={pathname.startsWith(`/tenants/${tenantId}/projects`)}
                    >
                      <Link href={`/tenants/${tenantId}/projects`}>
                        <FolderKanban className="h-4 w-4" />
                        Projects
                      </Link>
                    </SidebarMenuButton>
                    {tenantProjects.length > 0 ? (
                      <SidebarMenu className="mt-1 ml-5 space-y-0.5">
                        {tenantProjects.map((project) => (
                          <SidebarMenuItem key={project.project_id}>
                            <SidebarMenuButton
                              asChild
                              className="py-1.5 text-xs"
                              isActive={pathname.startsWith(`/tenants/${tenantId}/projects/${project.project_id}`)}
                            >
                              <Link href={`/tenants/${tenantId}/projects/${project.project_id}`}>{project.name}</Link>
                            </SidebarMenuButton>
                          </SidebarMenuItem>
                        ))}
                      </SidebarMenu>
                    ) : null}
                  </SidebarMenuItem>
                ) : null}
              </SidebarMenu>
              <SidebarMenu>
                <SidebarMenuLabel>Integrations</SidebarMenuLabel>
                {integrationItems.map((item) => {
                  const active = item.matchPrefix
                    ? pathname.startsWith(item.matchPrefix)
                    : pathname === item.href || pathname.startsWith(`${item.href}/`);
                  return (
                    <SidebarMenuItem key={item.href}>
                      <SidebarMenuButton asChild isActive={active}>
                        <Link href={item.href}>
                          <item.icon className="h-4 w-4" />
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
        <SidebarFooter>
          <Button asChild className="mb-2 w-full justify-start" variant="secondary">
            <Link href="/tenants/select">
              <FolderKanban className="mr-2 h-4 w-4" />
              Switch Tenant
            </Link>
          </Button>
          <Button
            className="w-full justify-start"
            variant="outline"
            onClick={() => {
              logout();
              router.push("/login");
            }}
          >
            <LogOut className="mr-2 h-4 w-4" />
            Logout
          </Button>
        </SidebarFooter>
      </Sidebar>

      <SidebarInset>
        <div className="mx-auto flex w-full max-w-none flex-col gap-4 px-4 py-4 md:px-6">{children}</div>
      </SidebarInset>
    </SidebarProvider>
  );
}
