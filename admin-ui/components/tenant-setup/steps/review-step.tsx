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
    <div className="space-y-3 text-sm">
      <p className="rounded-md border bg-muted/30 p-3">
        <strong>Tenant:</strong> {tenantDisplayId} ({tenantName || "-"})
      </p>
      <p className="rounded-md border bg-muted/30 p-3">
        <strong>Jira connection:</strong> {jiraConnectionId || "-"} | keys: {jiraProjectKeys || "-"}
      </p>
      <p className="rounded-md border bg-muted/30 p-3">
        <strong>GitHub installation:</strong> {githubInstallationId || "not connected"}
      </p>
      <p className="rounded-md border bg-muted/30 p-3">
        <strong>Repository:</strong> {repositoryUrl || "-"}
      </p>
      <div className="flex gap-2">
        <Button onClick={onSave} disabled={saving}>
          {saving ? "Saving..." : "Save Tenant"}
        </Button>
        {tenantDisplayId ? (
          <Button variant="outline" asChild>
            <Link href={`/tenants/${encodeURIComponent(tenantDisplayId)}/edit`}>Open Tenant Editor</Link>
          </Button>
        ) : null}
      </div>
    </div>
  );
}
