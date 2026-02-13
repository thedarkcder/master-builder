"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  listProjects,
  type ProjectRecord,
} from "@/lib/api";

export function TenantProjectsPage() {
  const params = useParams<{ tenantId: string }>();
  const { credentials, ready } = useAuth();

  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [statusLine, setStatusLine] = useState("");
  const [busy, setBusy] = useState(false);

  const activeProjects = useMemo(() => projects.filter((project) => !project.is_archived), [projects]);

  async function loadDashboard({ silent = false }: { silent?: boolean } = {}) {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      const loadedProjects = await listProjects(credentials, params.tenantId);
      setProjects(loadedProjects);
      if (!silent) {
        setStatusLine(`Loaded ${loadedProjects.length} project(s).`);
      }
    } catch (error) {
      setStatusLine(`Failed to load projects dashboard: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) {
      void loadDashboard({ silent: true });
    }
  }, [ready, credentials, params.tenantId]);

  return (
    <div className="w-full space-y-4">
      <Card className="w-full">
        <CardHeader className="space-y-3 md:flex md:flex-row md:items-start md:justify-between md:space-y-0">
          <div>
            <CardTitle>Projects</CardTitle>
            <CardDescription>Tenant project analytics and project list.</CardDescription>
          </div>
          <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
            <Button variant="outline" onClick={() => void loadDashboard()} disabled={busy}>
              Refresh
            </Button>
            <Button asChild>
              <Link href={`/tenants/${encodeURIComponent(params.tenantId)}/projects/new`}>Add project</Link>
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {statusLine ? <p className="rounded-md border px-3 py-2 text-sm text-muted-foreground">{statusLine}</p> : null}
          <div className="grid gap-3 lg:grid-cols-3">
            <Card>
              <CardHeader>
                <CardDescription>Total projects</CardDescription>
                <CardTitle>{projects.length}</CardTitle>
              </CardHeader>
            </Card>
            <Card>
              <CardHeader>
                <CardDescription>Active projects</CardDescription>
                <CardTitle>{activeProjects.length}</CardTitle>
              </CardHeader>
            </Card>
            <Card>
              <CardHeader>
                <CardDescription>Archived projects</CardDescription>
                <CardTitle>{projects.length - activeProjects.length}</CardTitle>
              </CardHeader>
            </Card>
          </div>

          <div className="space-y-2">
            {projects.length === 0 ? (
              <p className="rounded-md border p-3 text-sm text-muted-foreground">No projects yet.</p>
            ) : (
              projects.map((project) => (
                <Link
                  key={project.project_id}
                  href={`/tenants/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(project.project_id)}`}
                  className="block rounded-md border p-3 text-sm transition hover:bg-muted/30"
                >
                  <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0">
                      <p className="font-medium">{project.name}</p>
                      <p className="break-all text-muted-foreground">{project.github_repository}</p>
                    </div>
                    <p className="whitespace-nowrap text-xs font-medium text-muted-foreground">
                      {project.is_archived ? "Archived" : "Active"}
                    </p>
                  </div>
                  <p className="mt-1 text-sm">Jira: {project.jira_project_key}</p>
                </Link>
              ))
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
