"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import type { ComponentType } from "react";
import {
  Activity,
  Building2,
  FolderKanban,
  KeyRound,
  LayoutDashboard,
  LogOut,
  PlugZap,
  Settings2,
  ShieldCheck,
  MessageSquare,
  Webhook
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/components/auth-provider";
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
  { href: "/tenants", label: "Tenants", icon: Building2 },
  { href: "/runs", label: "Runs", icon: Activity },
  { href: "/secrets", label: "Secrets", icon: KeyRound }
];

export function DashboardShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { credentials, ready, logout } = useAuth();

  if (!ready) {
    return <main className="p-8 text-sm text-muted-foreground">Loading session...</main>;
  }

  if (!credentials) return null;

  if (pathname === "/tenants/select") {
    return <div className="min-h-screen bg-background">{children}</div>;
  }

  const tenantMatch = pathname.match(/^\/tenants\/([^/]+)\//);
  const tenantId = tenantMatch ? tenantMatch[1] : null;
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
          href: `/tenants/${tenantId}/edit/webhooks`,
          label: "Webhooks",
          icon: Webhook,
          matchPrefix: `/tenants/${tenantId}/edit/webhooks`
        },
        {
          href: `/tenants/${tenantId}/edit/access`,
          label: "Access Requests",
          icon: KeyRound,
          matchPrefix: `/tenants/${tenantId}/edit/access`
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
    ? navItems.filter((item) => !integrationItems.some((integrationsItem) => integrationsItem.href === item.href))
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
