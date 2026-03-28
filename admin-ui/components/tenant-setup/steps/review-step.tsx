import Link from "next/link";

import { Button } from "@/components/ui/button";

type ReviewStepProps = {
  tenantDisplayId: string;
  tenantName: string;
  jiraConnectionId: string | null;
  jiraProjectKeys: string;
  githubInstallationId: string | null;
  repositoryUrl: string;
  saving: boolean;
  onSave: () => void;
};

export function ReviewStep({
  tenantDisplayId,
  tenantName,
  jiraConnectionId,
  jiraProjectKeys,
  githubInstallationId,
  repositoryUrl,
  saving,
  onSave
}: ReviewStepProps) {
  return (
    <div className="space-y-6">
      <div className="grid gap-4 md:grid-cols-2">
        <div className="rounded-2xl border border-slate-200 bg-slate-50/70 px-5 py-5 text-sm">
          <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Workspace</div>
          <div className="mt-2 font-medium text-slate-900">{tenantName || "New workspace"}</div>
          <div className="mt-1 font-mono text-xs text-slate-600">{tenantDisplayId}</div>
        </div>
        <div className="rounded-2xl border border-slate-200 bg-slate-50/70 px-5 py-5 text-sm">
          <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Jira</div>
          <div className="mt-2 font-medium text-slate-900">{jiraConnectionId || "Not connected"}</div>
          <div className="mt-1 text-slate-600">{jiraProjectKeys || "No projects selected"}</div>
        </div>
        <div className="rounded-2xl border border-slate-200 bg-slate-50/70 px-5 py-5 text-sm">
          <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">GitHub</div>
          <div className="mt-2 font-medium text-slate-900">{githubInstallationId || "Not connected"}</div>
        </div>
        <div className="rounded-2xl border border-slate-200 bg-slate-50/70 px-5 py-5 text-sm">
          <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Repository</div>
          <div className="mt-2 break-all font-medium text-slate-900">{repositoryUrl || "Not selected"}</div>
        </div>
      </div>

      <div className="flex gap-2">
        <Button onClick={onSave} disabled={saving}>
          {saving ? "Saving..." : "Save workspace"}
        </Button>
        {tenantDisplayId ? (
          <Button variant="outline" asChild>
            <Link href={`/tenants/${encodeURIComponent(tenantDisplayId)}/edit`}>Open workspace settings</Link>
          </Button>
        ) : null}
      </div>
    </div>
  );
}
