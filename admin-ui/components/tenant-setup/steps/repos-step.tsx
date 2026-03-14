import type { GitHubRepositoryRecord } from "@/lib/api";
import type { TenantFormValues } from "@/lib/tenant-form";
import { Input } from "@/components/ui/input";

type ReposStepProps = {
  selectedRepoUrl: string;
  repositories: GitHubRepositoryRecord[];
  policy: TenantFormValues["policy"];
  onRepositoryChange: (url: string) => void;
  onPolicyChange: (policy: TenantFormValues["policy"]) => void;
};

export function ReposStep({
  selectedRepoUrl,
  repositories,
  policy,
  onRepositoryChange,
  onPolicyChange
}: ReposStepProps) {
  return (
    <div className="space-y-3">
      <div className="space-y-2">
        <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Repository</label>
        <select
          className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
          value={selectedRepoUrl}
          onChange={(event) => onRepositoryChange(event.target.value)}
          disabled={repositories.length === 0}
        >
          <option value="">{repositories.length === 0 ? "No repositories available" : "Select repository"}</option>
          {repositories.map((repo) => (
            <option key={repo.html_url} value={repo.html_url}>
              {repo.full_name}
            </option>
          ))}
        </select>
      </div>
      <div className="grid gap-3 md:grid-cols-3">
        <div className="space-y-2">
          <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Max runtime</label>
          <Input
            type="number"
            value={String(policy.max_runtime_minutes)}
            onChange={(event) =>
              onPolicyChange({
                ...policy,
                max_runtime_minutes: Number(event.target.value || 0)
              })
            }
          />
        </div>
        <div className="space-y-2">
          <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Max loops</label>
          <Input
            type="number"
            value={String(policy.max_dev_test_review_loops)}
            onChange={(event) =>
              onPolicyChange({
                ...policy,
                max_dev_test_review_loops: Number(event.target.value || 0)
              })
            }
          />
        </div>
        <div className="space-y-2">
          <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Max concurrency</label>
          <Input
            type="number"
            value={String(policy.max_concurrent_runs)}
            onChange={(event) =>
              onPolicyChange({
                ...policy,
                max_concurrent_runs: Number(event.target.value || 0)
              })
            }
          />
        </div>
      </div>
    </div>
  );
}
