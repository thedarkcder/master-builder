import { redirect } from "next/navigation";

export default async function TenantTeamIndexPage({ params }: { params: Promise<{ tenantId: string }> }) {
  const { tenantId } = await params;
  redirect(`/tenants/${encodeURIComponent(tenantId)}/team/members`);
}
