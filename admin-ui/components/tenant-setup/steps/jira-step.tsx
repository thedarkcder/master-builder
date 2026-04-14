import Link from "next/link";
import { CheckCircle2, KeyRound, Link2, RefreshCw } from "lucide-react";

import type { JiraProjectRecord } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type JiraStepProps = {
  connectionId: string | null;
  projectKeysText: string;
  jiraProjects: JiraProjectRecord[];
  selectedProjectKeys: Set<string>;
  onProjectKeysTextChange: (value: string) => void;
  onToggleJiraProject: (projectKey: string) => void;
  onStartJiraConnect: () => void;
  onLoadJiraProjects: () => void;
};

export function JiraStep({
  connectionId,
  projectKeysText,
  jiraProjects,
  selectedProjectKeys,
  onProjectKeysTextChange,
  onToggleJiraProject,
  onStartJiraConnect,
  onLoadJiraProjects
}: JiraStepProps) {
  return (
    <div className="space-y-3">
      <div className="rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">Before connecting Jira</p>
        <p>
          Save these secret refs in <strong>Secrets</strong>: <code className="font-mono">JIRA_OAUTH_CLIENT_ID</code>,{" "}
          <code className="font-mono">JIRA_OAUTH_CLIENT_SECRET</code>.
        </p>
      </div>
      <p className="rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
        <CheckCircle2 className="mr-1 inline h-4 w-4" />
        Jira connection: <strong>{connectionId ?? "not connected"}</strong>
      </p>
      <div className="flex flex-wrap gap-2">
        <Button asChild variant="outline">
          <Link href="/secrets">
            <KeyRound className="mr-2 h-4 w-4" />
            Open Secrets
          </Link>
        </Button>
        <Button onClick={onStartJiraConnect}>
          <Link2 className="mr-2 h-4 w-4" />
          Connect Jira
        </Button>
        <Button variant="outline" onClick={onLoadJiraProjects} disabled={!connectionId}>
          <RefreshCw className="mr-2 h-4 w-4" />
          Load Jira Projects
        </Button>
      </div>

      {jiraProjects.length > 0 ? (
        <div className="grid gap-2 md:grid-cols-2">
          {jiraProjects.map((project) => {
            const selected = selectedProjectKeys.has(project.key);
            return (
              <label key={project.key} className="flex items-center gap-2 rounded-md border p-2 text-sm">
                <input
                  type="checkbox"
                  checked={selected}
                  onChange={() => onToggleJiraProject(project.key)}
                  className="h-4 w-4 rounded border-input"
                />
                <span>
                  <strong>{project.key}</strong> - {project.name}
                </span>
              </label>
            );
          })}
        </div>
      ) : null}

      <div className="space-y-2">
        <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Project Keys</label>
        <Input value={projectKeysText} onChange={(event) => onProjectKeysTextChange(event.target.value)} placeholder="TP, APP" />
      </div>
    </div>
  );
}
