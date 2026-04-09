import { CheckCircle2, Link2, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";

type GitHubStepProps = {
  createdTenantId: string;
  tenantIdPreview: string;
  installationId: string | null;
  onStartInstall: () => void;
  onLoadRepositories: () => void;
};

export function GitHubStep({
  createdTenantId,
  tenantIdPreview,
  installationId,
  onStartInstall,
  onLoadRepositories
}: GitHubStepProps) {
  return (
    <div className="grid gap-6 xl:grid-cols-[320px_minmax(0,1fr)]">
      <div className="min-w-0 rounded-2xl border border-slate-200 bg-slate-50/70 px-5 py-5">
        <div className="space-y-4">
          <div className="flex items-center gap-2 text-sm font-medium text-slate-900">
            <CheckCircle2 className="h-4 w-4 text-emerald-600" />
            Workspace ready for install
          </div>
          <div className="text-sm text-slate-600">{createdTenantId || tenantIdPreview}</div>

          <div className="flex flex-col gap-2 pt-1">
            <Button onClick={onStartInstall}>
              <Link2 className="mr-2 h-4 w-4" />
              Install GitHub App
            </Button>
            <Button variant="outline" onClick={onLoadRepositories} disabled={!installationId}>
              <RefreshCw className="mr-2 h-4 w-4" />
              Load repositories
            </Button>
          </div>
        </div>
      </div>

      <div className="min-w-0 rounded-2xl border border-slate-200 bg-white px-5 py-5">
        <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Current installation</div>
        <div className="mt-3 text-sm font-medium text-slate-900">{installationId ?? "Not connected yet"}</div>
        <p className="mt-3 text-sm text-slate-500">
          After install, click Load repositories, then continue to Repository defaults. Jira-to-repository mapping is
          configured per project after workspace setup.
        </p>
      </div>
    </div>
  );
}
