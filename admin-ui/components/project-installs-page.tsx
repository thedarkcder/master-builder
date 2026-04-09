"use client";

import { ProjectInstallsContent } from "@/components/project-installs-content";
import { useAuth } from "@/components/auth-provider";

type ProjectInstallsPageProps = {
  tenantId: string;
  projectId: string;
};

export function ProjectInstallsPage({ tenantId, projectId }: ProjectInstallsPageProps) {
  const { credentials, ready } = useAuth();

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
        <h1 className="text-2xl font-semibold tracking-tight">Installed Integrations</h1>
        <p className="text-sm text-muted-foreground">
          Register project-scoped installs, define their allowed bindings, and fulfill install requests safely.
        </p>
      </div>
      <ProjectInstallsContent credentials={credentials} projectId={projectId} tenantId={tenantId} />
    </div>
  );
}
