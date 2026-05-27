"use client";

import { ProjectInstallsContent } from "@/components/project-installs-content";
import { ProjectSectionTabs } from "@/components/project-section-tabs";
import { useAuth } from "@/components/auth-provider";
import { canAccessPlatformAdmin, canManageProjects } from "@/lib/auth-routing";

type ProjectInstallsPageProps = {
  tenantId: string;
  projectId: string;
};

export function ProjectInstallsPage({ tenantId, projectId }: ProjectInstallsPageProps) {
  const { credentials, principal, ready } = useAuth();

  if (!ready) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-10">
        <p className="text-sm text-muted-foreground">Loading install settings…</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6 px-6 py-10">
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">Plugin Approvals</h1>
        <p className="text-sm text-muted-foreground">Approve the plugins a project run needs.</p>
      </div>
      <ProjectSectionTabs
        tenantId={tenantId}
        projectId={projectId}
        activeSection="installs"
        allowProjectManagement={canManageProjects(principal, tenantId)}
        isPlatformSuperAdmin={canAccessPlatformAdmin(principal)}
      />
      <ProjectInstallsContent credentials={credentials} projectId={projectId} tenantId={tenantId} />
    </div>
  );
}
