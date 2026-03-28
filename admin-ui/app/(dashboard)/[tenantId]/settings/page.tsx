import { redirect } from "next/navigation";

export default async function SettingsTenantRedirectPage({ params }: { params: Promise<{ tenantId: string }> }) {
  const { tenantId } = await params;
  redirect(`/${encodeURIComponent(tenantId)}/settings/integrations`);
}
