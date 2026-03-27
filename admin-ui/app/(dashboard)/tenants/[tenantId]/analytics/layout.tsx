"use client";

import Link from "next/link";
import { useEffect, useMemo } from "react";
import { useParams, usePathname, useRouter } from "next/navigation";

import { useAuth } from "@/components/auth-provider";

const technicalTabs = [
  { label: "Token Overview", href: (id: string) => `/tenants/${id}/analytics/token-overview` },
  { label: "Compare", href: (id: string) => `/tenants/${id}/analytics/token-compare` },
  { label: "Stage Diagnostics", href: (id: string) => `/tenants/${id}/analytics/stage-diagnostics` }
];

const businessTabs = [
  { label: "Delivery", href: (id: string) => `/tenants/${id}/analytics/business` }
];

export default function AnalyticsLayout({ children }: { children: React.ReactNode }) {
  const params = useParams<{ tenantId: string }>();
  const pathname = usePathname();
  const router = useRouter();
  const { principal } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);
  const membership = principal?.memberships.find((item) => item.tenant_id === tenantId);
  const isNonTechnical = membership?.effective_mode === "non_technical";
  const tabs = useMemo(
    () => (isNonTechnical ? businessTabs : [...businessTabs, ...technicalTabs]),
    [isNonTechnical]
  );

  useEffect(() => {
    if (isNonTechnical && pathname !== `/tenants/${tenantId}/analytics/business`) {
      router.replace(`/tenants/${encodeURIComponent(tenantId)}/analytics/business`);
    }
  }, [isNonTechnical, pathname, router, tenantId]);

  return (
    <div className="space-y-0">
      <div className="mb-1">
        <h1 className="text-xl font-semibold">Analytics</h1>
        <p className="text-sm text-muted-foreground">
          {isNonTechnical ? "Delivery progress, throughput, and recent completions." : "Delivery reporting plus token usage, trends, and stage diagnostics."}
        </p>
      </div>
      <div className="border-b overflow-x-auto">
        <nav className="-mb-px flex min-w-max gap-0" aria-label="Analytics tabs">
          {tabs.map((tab) => {
            const href = tab.href(tenantId);
            const active = pathname.startsWith(href);
            return (
              <Link
                key={tab.label}
                href={href}
                className={[
                  "inline-flex items-center whitespace-nowrap border-b-2 px-4 py-2.5 text-sm font-medium transition-colors",
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
