import { redirect } from "next/navigation";

type TenantProjectDiscordRouteParams = {
  tenantId: string;
  projectId: string;
};

export default async function TenantProjectDiscordRoute({
  params,
}: {
  params: Promise<TenantProjectDiscordRouteParams>;
}) {
  const resolved = await params;
  redirect(
    `/tenants/${encodeURIComponent(resolved.tenantId)}/projects/${encodeURIComponent(resolved.projectId)}/notifications`,
  );
}
