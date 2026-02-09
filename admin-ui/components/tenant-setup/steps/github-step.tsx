import Link from "next/link";
import { CheckCircle2, KeyRound, Link2, RefreshCw } from "lucide-react";

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
    <div className="space-y-3">
      <div className="rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">Before installing GitHub App</p>
        <p>
          Required secret ref in <strong>Secrets</strong>: <code className="font-mono">GITHUB_APP_SLUG</code>
        </p>
        <p>
          Required secret refs in <strong>Secrets</strong>: <code className="font-mono">GITHUB_APP_ID</code>,{" "}
          <code className="font-mono">GITHUB_CLIENT_SECRET</code>
        </p>
        <p>
          Optional webhook secret ref: <code className="font-mono">GITHUB_WEBHOOK_SECRET</code>
        </p>
      </div>
      <p className="rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
        <CheckCircle2 className="mr-1 inline h-4 w-4" />
        Tenant <strong>{createdTenantId || tenantIdPreview}</strong> is ready for GitHub install.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button asChild variant="outline">
          <Link href="/secrets">
            <KeyRound className="mr-2 h-4 w-4" />
            Open Secrets
          </Link>
        </Button>
        <Button onClick={onStartInstall}>
          <Link2 className="mr-2 h-4 w-4" />
          Install GitHub App
        </Button>
        <Button variant="outline" onClick={onLoadRepositories} disabled={!installationId}>
          <RefreshCw className="mr-2 h-4 w-4" />
          Load Repositories
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        Current installation ID: <strong>{installationId ?? "not connected yet"}</strong>
      </p>
    </div>
  );
}
