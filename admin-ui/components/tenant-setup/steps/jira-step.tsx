import { CheckCircle2, Link2, RefreshCw } from "lucide-react";

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
    <div className="grid gap-6 xl:grid-cols-[320px_minmax(0,1fr)]">
      <div className="min-w-0 rounded-2xl border border-slate-200 bg-slate-50/70 px-5 py-5">
        <div className="space-y-4">
          <div className="flex items-center gap-2 text-sm font-medium text-slate-900">
            <CheckCircle2 className="h-4 w-4 text-emerald-600" />
            Atlassian connection
          </div>
          <div className="text-sm text-slate-600">{connectionId ?? "Not connected yet"}</div>
          <div className="flex flex-col gap-2 pt-1">
            <Button onClick={onStartJiraConnect}>
              <Link2 className="mr-2 h-4 w-4" />
              Connect Atlassian
            </Button>
            <Button variant="outline" onClick={onLoadJiraProjects} disabled={!connectionId}>
              <RefreshCw className="mr-2 h-4 w-4" />
              Load projects
            </Button>
          </div>
        </div>
      </div>

      <div className="min-w-0 space-y-5">
        <div className="flex items-center justify-between gap-3">
          <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Projects</div>
          {jiraProjects.length > 0 ? <div className="text-sm text-slate-500">{selectedProjectKeys.size} selected</div> : null}
        </div>

        {jiraProjects.length > 0 ? (
          <div className="grid gap-3 md:grid-cols-2">
            {jiraProjects.map((project) => {
              const selected = selectedProjectKeys.has(project.key);
              return (
                <label
                  key={project.key}
                  className={[
                    "flex items-center gap-3 rounded-2xl px-4 py-3 text-sm ring-1 transition-colors",
                    selected ? "bg-slate-950 text-white ring-slate-950" : "bg-white text-slate-800 ring-slate-200",
                  ].join(" ")}
                >
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
        ) : (
          <div className="rounded-2xl bg-slate-50 px-4 py-4 text-sm text-slate-600 ring-1 ring-slate-200">
            Load projects after connecting Atlassian.
          </div>
        )}

        <div className="space-y-2 border-t border-slate-200 pt-5">
          <label className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Project keys</label>
          <Input
            value={projectKeysText}
            onChange={(event) => onProjectKeysTextChange(event.target.value)}
            placeholder="TP, APP"
            className="h-12 rounded-2xl border-slate-200 bg-white"
          />
          <p className="text-sm text-slate-500">Use this field only if you need to paste keys manually.</p>
        </div>
      </div>
    </div>
  );
}
