import { ProjectInstallsPage } from "@/components/project-installs-page";

type TenantProjectInstallsRouteProps = {
  params: Promise<{
    tenantId: string;
    projectId: string;
  }>;
};

export default async function TenantProjectInstallsRoute({ params }: TenantProjectInstallsRouteProps) {
  const { tenantId, projectId } = await params;
  return <ProjectInstallsPage projectId={projectId} tenantId={tenantId} />;
}
