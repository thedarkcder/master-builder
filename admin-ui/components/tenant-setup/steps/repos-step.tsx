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
  const runtimeMinutes = policy.max_runtime_minutes ?? 120;
  const reviewLoops = policy.max_dev_test_review_loops ?? 2;
  const concurrency = policy.max_concurrent_runs ?? 2;

  return (
    <div className="space-y-6">
      <div className="space-y-3">
        <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Repository default</div>
        <select
          className="h-12 w-full rounded-2xl border border-slate-200 bg-white px-4 text-sm"
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
        <p className="text-xs text-slate-500">
          This is a workspace default. Jira-project-to-repository mappings are configured per project.
        </p>
      </div>

      <div className="rounded-2xl border border-slate-200 bg-slate-50/70 px-5 py-5">
        <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Run limits</div>
        <div className="mt-4 grid gap-4 md:grid-cols-3">
          <div className="space-y-2">
            <label className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Runtime</label>
            <Input
              type="number"
              value={String(runtimeMinutes)}
              onChange={(event) =>
                onPolicyChange({
                  ...policy,
                  max_runtime_minutes: Number(event.target.value || 0)
                })
              }
              className="h-12 rounded-2xl border-slate-200 bg-white"
            />
          </div>
          <div className="space-y-2">
            <label className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Loops</label>
            <Input
              type="number"
              value={String(reviewLoops)}
              onChange={(event) =>
                onPolicyChange({
                  ...policy,
                  max_dev_test_review_loops: Number(event.target.value || 0)
                })
              }
              className="h-12 rounded-2xl border-slate-200 bg-white"
            />
          </div>
          <div className="space-y-2">
            <label className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Concurrency</label>
            <Input
              type="number"
              value={String(concurrency)}
              onChange={(event) =>
                onPolicyChange({
                  ...policy,
                  max_concurrent_runs: Number(event.target.value || 0)
                })
              }
              className="h-12 rounded-2xl border-slate-200 bg-white"
            />
          </div>
        </div>
      </div>
    </div>
  );
}
