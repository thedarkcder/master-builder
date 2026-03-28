"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { Archive, CheckCircle2, ChevronRight, FolderKanban, Plus, RefreshCw } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { listProjects, type ProjectRecord } from "@/lib/api";
import { canManageProjects } from "@/lib/auth-routing";

function projectInitials(name: string): string {
  const words = name.trim().split(/\s+/);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  return name.slice(0, 2).toUpperCase();
}

function projectAvatarColor(id: string): string {
  const colors = [
    "bg-violet-600",
    "bg-blue-600",
    "bg-cyan-600",
    "bg-emerald-600",
    "bg-amber-600",
    "bg-rose-600",
    "bg-pink-600",
    "bg-indigo-600"
  ];
  let hash = 0;
  for (let i = 0; i < id.length; i++) {
    hash = (hash * 31 + id.charCodeAt(i)) & 0xffffffff;
  }
  return colors[Math.abs(hash) % colors.length];
}

export function TenantProjectsPage() {
  const params = useParams<{ tenantId: string }>();
  const { credentials, ready, principal } = useAuth();

  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [statusLine, setStatusLine] = useState("");
  const [busy, setBusy] = useState(false);

  const activeProjects = useMemo(() => projects.filter((p) => !p.is_archived), [projects]);
  const archivedProjects = useMemo(() => projects.filter((p) => p.is_archived), [projects]);
  const allowProjectManagement = canManageProjects(principal, params.tenantId);

  async function loadDashboard({ silent = false }: { silent?: boolean } = {}) {
    if (!credentials) return;
    setBusy(true);
    try {
      const loaded = await listProjects(credentials, params.tenantId);
      setProjects(loaded);
      if (!silent) setStatusLine(`Loaded ${loaded.length} project(s).`);
    } catch (error) {
      setStatusLine(`Failed to load projects: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) void loadDashboard({ silent: true });
  }, [ready, credentials, params.tenantId]);

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Projects</h1>
          <p className="text-sm text-muted-foreground">Manage projects linked to GitHub and Jira.</p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => void loadDashboard()} disabled={busy}>
            <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
            Refresh
          </Button>
          {allowProjectManagement ? (
            <Button asChild size="sm">
              <Link href={`/tenants/${encodeURIComponent(params.tenantId)}/projects/new`}>
                <Plus className="mr-1.5 h-3.5 w-3.5" />
                Add project
              </Link>
            </Button>
          ) : null}
        </div>
      </div>

      {statusLine ? (
        <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p>
      ) : null}

      {/* KPI cards */}
      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader className="flex flex-row items-start justify-between space-y-0 pb-2">
            <p className="text-sm font-medium text-muted-foreground">Total</p>
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10">
              <FolderKanban className="h-4 w-4 text-primary" />
            </div>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{projects.length}</p>
            <p className="mt-1 text-xs text-muted-foreground">All projects</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-start justify-between space-y-0 pb-2">
            <p className="text-sm font-medium text-muted-foreground">Active</p>
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-success/10">
              <CheckCircle2 className="h-4 w-4 text-success" />
            </div>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{activeProjects.length}</p>
            <p className="mt-1 text-xs text-muted-foreground">Currently active</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-start justify-between space-y-0 pb-2">
            <p className="text-sm font-medium text-muted-foreground">Archived</p>
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-muted">
              <Archive className="h-4 w-4 text-muted-foreground" />
            </div>
          </CardHeader>
          <CardContent>
            <p className="text-2xl font-bold">{archivedProjects.length}</p>
            <p className="mt-1 text-xs text-muted-foreground">No longer active</p>
          </CardContent>
        </Card>
      </div>

      {/* Project list */}
      {projects.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-12 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted mb-4">
              <FolderKanban className="h-6 w-6 text-muted-foreground" />
            </div>
            <p className="font-medium">No projects yet</p>
            <p className="mt-1 text-sm text-muted-foreground">Create your first project to start orchestrating runs.</p>
            {allowProjectManagement ? (
              <Button asChild className="mt-4" size="sm">
                <Link href={`/tenants/${encodeURIComponent(params.tenantId)}/projects/new`}>
                  <Plus className="mr-1.5 h-3.5 w-3.5" />
                  Add project
                </Link>
              </Button>
            ) : null}
          </CardContent>
        </Card>
      ) : (
        <div className="overflow-hidden rounded-xl border bg-card shadow-sm">
          <ul className="divide-y">
            {projects.map((project) => (
              <li key={project.project_id}>
                <Link
                  href={`/tenants/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(project.project_id)}`}
                  className="flex items-center gap-3 px-4 py-3.5 transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:bg-muted/50"
                >
                  {/* Avatar */}
                  <div
                    className={`flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg text-xs font-bold text-white ${projectAvatarColor(project.project_id)}`}
                  >
                    {projectInitials(project.name)}
                  </div>

                  {/* Info */}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <p className="truncate font-medium text-sm">{project.name}</p>
                      <Badge variant={project.is_archived ? "outline" : "success"} className="text-[10px] px-1.5 py-0">
                        {project.is_archived ? "Archived" : "Active"}
                      </Badge>
                    </div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                      {project.jira_project_key ? (
                        <span className="rounded bg-muted px-1.5 py-0.5 font-mono">{project.jira_project_key}</span>
                      ) : null}
                      {project.github_repository ? (
                        <span className="truncate max-w-[260px]">
                          {project.github_repository.replace(/^https?:\/\/github\.com\//, "")}
                        </span>
                      ) : null}
                    </div>
                  </div>

                  <ChevronRight className="h-4 w-4 flex-shrink-0 text-muted-foreground" />
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
