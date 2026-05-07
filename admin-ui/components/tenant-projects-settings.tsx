"use client";

import { useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { ProjectsManager } from "@/components/projects-manager";
import { useToast } from "@/components/ui/toast-provider";
import {
  createProject,
  listGitHubRepositories,
  listJiraProjects,
  listProjects,
  updateProjectArchiveState,
  updateProjectConfiguration,
  type GitHubRepositoryRecord,
  type JiraProjectRecord,
  type ProjectArchiveUpdatePayload,
  type ProjectConfigurationUpdatePayload,
  type ProjectCreatePayload,
  type ProjectRecord,
  type TenantRecord
} from "@/lib/api";

type TenantProjectsSettingsProps = {
  tenant: TenantRecord;
};

export function TenantProjectsSettings({ tenant }: TenantProjectsSettingsProps) {
  const { credentials } = useAuth();
  const { showToast } = useToast();
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [repositories, setRepositories] = useState<GitHubRepositoryRecord[]>([]);
  const [jiraProjects, setJiraProjects] = useState<JiraProjectRecord[]>([]);
  const [busy, setBusy] = useState(false);
  const [repositoriesLoading, setRepositoriesLoading] = useState(false);

  async function loadProjects(): Promise<void> {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      setProjects(await listProjects(credentials, tenant.tenant_id));
    } catch (error) {
      showToast({ title: "Project list failed", description: (error as Error).message, tone: "error" });
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void loadProjects();
  }, [credentials, tenant.tenant_id]);

  async function refreshProjectSources(): Promise<void> {
    if (!credentials) {
      return;
    }
    setRepositoriesLoading(true);
    try {
      const repos = await listGitHubRepositories(credentials, tenant.tenant_id);
      setRepositories(repos);
      if (tenant.jira.connection_id) {
        setJiraProjects(await listJiraProjects(credentials, tenant.jira.connection_id));
      }
      showToast({ title: "Project option sources refreshed", tone: "success" });
    } catch (error) {
      showToast({ title: "Project option refresh failed", description: (error as Error).message, tone: "error" });
    } finally {
      setRepositoriesLoading(false);
    }
  }

  async function handleCreateProject(payload: ProjectCreatePayload): Promise<void> {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      await createProject(credentials, tenant.tenant_id, payload);
      setProjects(await listProjects(credentials, tenant.tenant_id));
      showToast({ title: "Project created", description: payload.name, tone: "success" });
    } catch (error) {
      showToast({ title: "Project create failed", description: (error as Error).message, tone: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function handleUpdateProjectConfiguration(
    projectId: string,
    payload: ProjectConfigurationUpdatePayload
  ): Promise<void> {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      await updateProjectConfiguration(credentials, tenant.tenant_id, projectId, payload);
      setProjects(await listProjects(credentials, tenant.tenant_id));
      showToast({ title: "Project updated", description: projectId, tone: "success" });
    } catch (error) {
      showToast({ title: "Project update failed", description: (error as Error).message, tone: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function handleUpdateProjectArchiveState(
    projectId: string,
    payload: ProjectArchiveUpdatePayload
  ): Promise<void> {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      await updateProjectArchiveState(credentials, tenant.tenant_id, projectId, payload);
      setProjects(await listProjects(credentials, tenant.tenant_id));
      showToast({
        title: payload.is_archived ? "Project archived" : "Project unarchived",
        description: projectId,
        tone: "success"
      });
    } catch (error) {
      showToast({ title: "Project archive update failed", description: (error as Error).message, tone: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="overflow-hidden rounded-2xl border bg-background">
      <div className="px-6 pt-6">
        <h2 className="text-base font-semibold">Projects</h2>
      </div>
      <div className="p-6">
        <ProjectsManager
          projects={projects}
          repositories={repositories}
          jiraProjects={jiraProjects}
          busy={busy || repositoriesLoading}
          onRefreshOptions={() => void refreshProjectSources()}
          onCreateProject={handleCreateProject}
          onUpdateProjectConfiguration={handleUpdateProjectConfiguration}
          onUpdateProjectArchiveState={handleUpdateProjectArchiveState}
        />
      </div>
    </div>
  );
}
