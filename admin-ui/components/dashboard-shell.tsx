"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Activity, Building2, KeyRound, LogOut, PlusSquare } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useAuth } from "@/components/auth-provider";

const navItems = [
  { href: "/tenants", label: "Tenants", icon: Building2 },
  { href: "/tenants/new", label: "New Tenant", icon: PlusSquare },
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

  return (
    <div className="min-h-screen bg-background">
      <div className="mx-auto flex max-w-7xl flex-col gap-4 px-4 py-4 md:px-6">
        <header className="rounded-lg border bg-card p-4 shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground">Master Builder</p>
              <h1 className="text-xl font-semibold tracking-tight">Operations Dashboard</h1>
              <p className="text-xs text-muted-foreground">Connected to {credentials.apiBaseUrl}</p>
            </div>
            <Button
              variant="outline"
              onClick={() => {
                logout();
                router.push("/login");
              }}
            >
              <LogOut className="mr-2 h-4 w-4" />
              Logout
            </Button>
          </div>
        </header>

        <div className="grid gap-4 md:grid-cols-[220px_1fr]">
          <aside className="rounded-lg border bg-card p-3 shadow-sm">
            <nav className="space-y-1">
              {navItems.map((item) => {
                const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={cn(
                      "flex items-center gap-2 rounded-md px-3 py-2 text-sm transition",
                      active ? "bg-primary text-primary-foreground" : "hover:bg-muted"
                    )}
                  >
                    <item.icon className="h-4 w-4" />
                    {item.label}
                  </Link>
                );
              })}
            </nav>
          </aside>
          <main>{children}</main>
        </div>
      </div>
    </div>
  );
}
