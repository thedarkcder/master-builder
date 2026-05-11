"use client";

import { useParams } from "next/navigation";

import { useAuth } from "@/components/auth-provider";
import { ProjectAppAdminPage } from "@/components/project-apps/project-app-admin-page";

export default function TenantProjectDeploymentAdminRoute() {
  const params = useParams<{ tenantId: string; projectId: string; deploymentId: string }>();
  const { credentials, ready } = useAuth();

  if (!ready) {
    return <main className="p-8 text-sm text-muted-foreground">Loading session...</main>;
  }

  return (
    <ProjectAppAdminPage
      tenantId={params.tenantId}
      projectId={params.projectId}
      appId={params.deploymentId}
      credentials={credentials}
    />
  );
}
