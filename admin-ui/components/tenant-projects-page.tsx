"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { Archive, CheckCircle2, ChevronRight, FolderKanban, Plus, RefreshCw } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
      if (!silent) {
        const activeCount = loaded.filter((project) => !project.is_archived).length;
        setStatusLine(`Loaded ${activeCount} active project(s).`);
      }
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
              <Link href={`/${encodeURIComponent(params.tenantId)}/projects/new`}>
                <Plus className="mr-1.5 h-3.5 w-3.5" />
                Add project
              </Link>
            </Button>
          ) : null}
        </div>
      </div>

      {statusLine ? (
        <div className="rounded-xl border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">{statusLine}</div>
      ) : null}

      <div className="grid gap-4 sm:grid-cols-3">
        {[
          { label: "Total", value: projects.length, sub: "All projects", icon: <FolderKanban className="h-4 w-4 text-primary" />, iconBg: "bg-primary/10" },
          { label: "Active", value: activeProjects.length, sub: "Currently active", icon: <CheckCircle2 className="h-4 w-4 text-success" />, iconBg: "bg-success/10" },
          { label: "Archived", value: archivedProjects.length, sub: "No longer active", icon: <Archive className="h-4 w-4 text-muted-foreground" />, iconBg: "bg-muted" },
        ].map((item) => (
          <div key={item.label} className="rounded-xl border px-4 py-3">
            <div className="flex items-start justify-between">
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">{item.label}</p>
              <div className={`flex h-8 w-8 items-center justify-center rounded-lg ${item.iconBg}`}>{item.icon}</div>
            </div>
            <p className="mt-1 text-2xl font-bold">{item.value}</p>
            <p className="mt-1 text-xs text-muted-foreground">{item.sub}</p>
          </div>
        ))}
      </div>

      {activeProjects.length === 0 ? (
        <div className="overflow-hidden rounded-2xl border bg-background">
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted mb-4">
              <FolderKanban className="h-6 w-6 text-muted-foreground" />
            </div>
            <p className="font-medium">No active projects</p>
            <p className="mt-1 text-sm text-muted-foreground">Create a project to start orchestrating runs in this workspace.</p>
            {allowProjectManagement ? (
              <Button asChild className="mt-4" size="sm">
                <Link href={`/${encodeURIComponent(params.tenantId)}/projects/new`}>
                  <Plus className="mr-1.5 h-3.5 w-3.5" />
                  Add project
                </Link>
              </Button>
            ) : null}
          </div>
        </div>
      ) : (
        <div className="overflow-hidden rounded-2xl border bg-background">
          <ul className="divide-y">
            {activeProjects.map((project) => (
              <li key={project.project_id}>
                <Link
                  href={`/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(project.project_id)}`}
                  className="flex items-center gap-3 px-5 py-3.5 transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:bg-muted/50"
                >
                  <div
                    className={`flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg text-xs font-bold text-white ${projectAvatarColor(project.project_id)}`}
                  >
                    {projectInitials(project.name)}
                  </div>
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
