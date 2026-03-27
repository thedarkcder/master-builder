"use client";

import { useEffect } from "react";
import { useParams, useRouter } from "next/navigation";

import { useAuth } from "@/components/auth-provider";

export default function TenantAnalyticsPage() {
  const router = useRouter();
  const params = useParams<{ tenantId: string }>();
  const { principal, ready } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);

  useEffect(() => {
    if (!ready) {
      return;
    }
    const membership = principal?.memberships.find((item) => item.tenant_id === tenantId);
    const target =
      membership?.effective_mode === "non_technical"
        ? `/tenants/${encodeURIComponent(tenantId)}/analytics/business`
        : `/tenants/${encodeURIComponent(tenantId)}/analytics/token-overview`;
    router.replace(target);
  }, [principal, ready, router, tenantId]);

  return <div className="text-sm text-muted-foreground">Loading analytics…</div>;
}
