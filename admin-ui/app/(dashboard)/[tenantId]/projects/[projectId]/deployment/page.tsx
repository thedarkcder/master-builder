"use client";

import { useParams } from "next/navigation";

import { ProjectDeploymentPolicyPage } from "@/components/project-deployment-policy-page";

export default function TenantProjectDeploymentRoute() {
  const params = useParams<{ tenantId: string; projectId: string }>();

  return <ProjectDeploymentPolicyPage tenantId={params.tenantId} projectId={params.projectId} />;
}
