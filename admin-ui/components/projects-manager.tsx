"use client";

import { useMemo, useState } from "react";
import { RefreshCcw } from "lucide-react";

import type {
  GitHubRepositoryRecord,
  JiraProjectRecord,
  ProjectCreatePayload,
  ProjectRecord,
  ProjectUpdatePayload
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

type Props = {
  projects: ProjectRecord[];
  repositories: GitHubRepositoryRecord[];
  jiraProjects: JiraProjectRecord[];
  busy: boolean;
  onRefreshOptions: () => void;
  onCreateProject: (payload: ProjectCreatePayload) => Promise<void>;
  onUpdateProject: (projectId: string, payload: ProjectUpdatePayload) => Promise<void>;
};

type Draft = {
  name: string;
  github_repository: string;
  jira_project_key: string;
};

function emptyDraft(): Draft {
  return { name: "", github_repository: "", jira_project_key: "" };
}

export function ProjectsManager({
  projects,
  repositories,
  jiraProjects,
  busy,
  onRefreshOptions,
  onCreateProject,
  onUpdateProject
}: Props) {
  const [createDraft, setCreateDraft] = useState<Draft>(emptyDraft);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState<Draft>(emptyDraft);

  const activeCount = useMemo(() => projects.filter((project) => !project.is_archived).length, [projects]);

  async function handleCreate() {
    if (!createDraft.name.trim() || !createDraft.github_repository.trim() || !createDraft.jira_project_key.trim()) {
      return;
    }
    await onCreateProject({
      name: createDraft.name.trim(),
      github_repository: createDraft.github_repository.trim(),
      jira_project_key: createDraft.jira_project_key.trim().toUpperCase(),
      policy_overrides: {}
    });
    setCreateDraft(emptyDraft());
  }

  function startEdit(project: ProjectRecord) {
    setEditingId(project.project_id);
    setEditDraft({
      name: project.name,
      github_repository: project.github_repository,
      jira_project_key: project.jira_project_key
    });
  }

  async function saveEdit(project: ProjectRecord) {
    if (!editDraft.name.trim() || !editDraft.github_repository.trim() || !editDraft.jira_project_key.trim()) {
      return;
    }
    await onUpdateProject(project.project_id, {
      name: editDraft.name.trim(),
      github_repository: editDraft.github_repository.trim(),
      jira_project_key: editDraft.jira_project_key.trim().toUpperCase(),
      policy_overrides: project.policy_overrides ?? {},
      is_archived: project.is_archived
    });
    setEditingId(null);
    setEditDraft(emptyDraft());
  }

  async function toggleArchive(project: ProjectRecord) {
    await onUpdateProject(project.project_id, {
      name: project.name,
      github_repository: project.github_repository,
      jira_project_key: project.jira_project_key,
      policy_overrides: project.policy_overrides ?? {},
      is_archived: !project.is_archived
    });
  }

  return (
    <div className="space-y-4">
      <div className="rounded-md border p-3 text-sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="font-medium">Projects ({activeCount} active / {projects.length} total)</p>
          <Button variant="outline" size="sm" disabled={busy} onClick={onRefreshOptions}>
            <RefreshCcw className="mr-2 h-4 w-4" />
            Refresh options
          </Button>
        </div>
        <p className="mt-1 text-muted-foreground">Each project maps exactly one repository and one Jira project key.</p>
      </div>

      <div className="grid gap-3 rounded-md border p-3 md:grid-cols-3">
        <div className="space-y-1">
          <Label htmlFor="project-create-name">Project name</Label>
          <Input
            id="project-create-name"
            value={createDraft.name}
            onChange={(event) => setCreateDraft((prev) => ({ ...prev, name: event.target.value }))}
            placeholder="Mobile App"
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="project-create-repo">Repository</Label>
          <Input
            id="project-create-repo"
            list="project-repositories"
            value={createDraft.github_repository}
            onChange={(event) => setCreateDraft((prev) => ({ ...prev, github_repository: event.target.value }))}
            placeholder="https://github.com/org/repo"
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="project-create-jira">Jira key</Label>
          <Input
            id="project-create-jira"
            list="project-jira-keys"
            value={createDraft.jira_project_key}
            onChange={(event) => setCreateDraft((prev) => ({ ...prev, jira_project_key: event.target.value.toUpperCase() }))}
            placeholder="APP"
          />
        </div>
        <div className="md:col-span-3">
          <Button size="sm" disabled={busy} onClick={() => void handleCreate()}>
            Add project
          </Button>
        </div>
        <datalist id="project-repositories">
          {repositories.map((repo) => (
            <option key={repo.full_name} value={repo.html_url}>
              {repo.full_name}
            </option>
          ))}
        </datalist>
        <datalist id="project-jira-keys">
          {jiraProjects.map((project) => (
            <option key={project.key} value={project.key}>
              {project.name}
            </option>
          ))}
        </datalist>
      </div>

      <ul className="space-y-2">
        {projects.map((project) => {
          const editing = editingId === project.project_id;
          return (
            <li key={project.project_id} className={cn("rounded-md border p-3", project.is_archived ? "bg-muted/30" : "")}>
              {editing ? (
                <div className="grid gap-2 md:grid-cols-3">
                  <Input value={editDraft.name} onChange={(event) => setEditDraft((prev) => ({ ...prev, name: event.target.value }))} />
                  <Input value={editDraft.github_repository} onChange={(event) => setEditDraft((prev) => ({ ...prev, github_repository: event.target.value }))} />
                  <Input value={editDraft.jira_project_key} onChange={(event) => setEditDraft((prev) => ({ ...prev, jira_project_key: event.target.value.toUpperCase() }))} />
                  <div className="md:col-span-3 flex flex-wrap gap-2">
                    <Button size="sm" onClick={() => void saveEdit(project)} disabled={busy}>
                      Save
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => setEditingId(null)}>
                      Cancel
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="space-y-2 text-sm">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="font-medium">{project.name}</p>
                    <div className="flex flex-wrap gap-2">
                      <Button size="sm" variant="outline" onClick={() => startEdit(project)}>
                        Edit
                      </Button>
                      <Button size="sm" variant="outline" disabled={busy} onClick={() => void toggleArchive(project)}>
                        {project.is_archived ? "Unarchive" : "Archive"}
                      </Button>
                    </div>
                  </div>
                  <p>
                    <strong>Repository:</strong> {project.github_repository}
                  </p>
                  <p>
                    <strong>Jira key:</strong> {project.jira_project_key}
                  </p>
                  <p>
                    <strong>Status:</strong> {project.is_archived ? "Archived" : "Active"}
                  </p>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
