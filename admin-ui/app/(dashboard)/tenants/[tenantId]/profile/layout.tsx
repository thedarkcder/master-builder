"use client";

import Link from "next/link";
import { useParams, usePathname } from "next/navigation";

const tabs = [
  { label: "Profile", href: "" },
  { label: "Security", href: "/security" },
];

export default function TenantProfileLayout({ children }: { children: React.ReactNode }) {
  const params = useParams<{ tenantId: string }>();
  const pathname = usePathname();
  const tenantId = decodeURIComponent(params.tenantId);
  const encodedTenantId = encodeURIComponent(tenantId);

  return (
    <div className="space-y-0">
      <div className="mb-1">
        <h1 className="text-xl font-semibold">Profile</h1>
        <p className="text-sm text-muted-foreground">Manage your identity, workspace preferences, and account security.</p>
      </div>
      <div className="overflow-x-auto border-b">
        <nav className="-mb-px flex min-w-max gap-0" aria-label="Profile tabs">
          {tabs.map((tab) => {
            const href = `/tenants/${encodedTenantId}/profile${tab.href}`;
            const active = pathname === href || pathname.startsWith(`${href}/`);
            return (
              <Link
                key={href}
                href={href}
                className={[
                  "inline-flex items-center border-b-2 px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors",
                  active
                    ? "border-primary text-foreground"
                    : "border-transparent text-muted-foreground hover:border-border hover:text-foreground",
                ].join(" ")}
              >
                {tab.label}
              </Link>
            );
          })}
        </nav>
      </div>
      <div className="pt-6">{children}</div>
    </div>
  );
}
