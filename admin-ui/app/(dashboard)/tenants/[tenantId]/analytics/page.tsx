import { redirect } from "next/navigation";

type TenantAnalyticsLandingParams = {
  tenantId: string;
};

export default async function TenantAnalyticsPage({
  params
}: {
  params: Promise<TenantAnalyticsLandingParams>;
}) {
  const resolved = await params;
  redirect(`/tenants/${encodeURIComponent(resolved.tenantId)}/analytics/token-overview`);
}
