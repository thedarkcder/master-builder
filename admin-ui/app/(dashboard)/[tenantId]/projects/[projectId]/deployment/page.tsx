import { redirect } from "next/navigation";

export default async function TenantProjectDeploymentRoute({
  params,
}: {
  params: Promise<{ tenantId: string; projectId: string }>;
}) {
  const resolved = await params;
  redirect(`/${encodeURIComponent(resolved.tenantId)}/projects/${encodeURIComponent(resolved.projectId)}/apps`);
}
