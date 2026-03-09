"use client";

import Link from "next/link";
import { useParams, usePathname } from "next/navigation";

const tabs = [
  { label: "Token Overview", href: (id: string) => `/tenants/${id}/analytics/token-overview` },
  { label: "Compare", href: (id: string) => `/tenants/${id}/analytics/token-compare` },
  { label: "Stage Diagnostics", href: (id: string) => `/tenants/${id}/analytics/stage-diagnostics` }
];

export default function AnalyticsLayout({ children }: { children: React.ReactNode }) {
  const params = useParams<{ tenantId: string }>();
  const pathname = usePathname();
  const tenantId = decodeURIComponent(params.tenantId);

  return (
    <div className="space-y-0">
      <div className="mb-1">
        <h1 className="text-xl font-semibold">Analytics</h1>
        <p className="text-sm text-muted-foreground">Token usage, trends, and stage diagnostics.</p>
      </div>
      <div className="border-b">
        <nav className="-mb-px flex gap-0" aria-label="Analytics tabs">
          {tabs.map((tab) => {
            const href = tab.href(tenantId);
            const active = pathname.startsWith(href);
            return (
              <Link
                key={tab.label}
                href={href}
                className={[
                  "inline-flex items-center border-b-2 px-4 py-2.5 text-sm font-medium transition-colors",
                  active
                    ? "border-primary text-foreground"
                    : "border-transparent text-muted-foreground hover:border-border hover:text-foreground"
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
