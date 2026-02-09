import type { ReactNode } from "react";

import type { GitHubRepositoryRecord } from "@/lib/api";
import type { TenantFormValues } from "@/lib/tenant-form";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

const DISCORD_NOTIFY_EVENT_OPTIONS = [
  {
    value: "lock_acquired",
    label: "Run queued",
    description: "Post when a run is accepted and lock is acquired."
  },
  {
    value: "plan_posted",
    label: "Plan posted",
    description: "Post when the PM plan is produced for the run."
  },
  {
    value: "pr_opened",
    label: "PR opened",
    description: "Post when a pull request is opened."
  },
  {
    value: "run_failed",
    label: "Run failed",
    description: "Post when a run fails and needs intervention."
  },
  {
    value: "review_signal",
    label: "Review signal",
    description: "Post PR-ready or review-required signals from GitHub webhook events."
  },
  {
    value: "decision_gate_required",
    label: "Decision gate required",
    description: "Post when a run is blocked pending PM/BA clarification."
  }
] as const;

function SectionFrame({ title, description, children }: { title: string; description: string; children: ReactNode }) {
  return (
    <section className="space-y-3">
      <div className="space-y-1">
        <h3 className="text-sm font-semibold">{title}</h3>
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>
      {children}
    </section>
  );
}

function FieldLabel({ children }: { children: ReactNode }) {
  return <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{children}</label>;
}

function Toggle({
  label,
  checked,
  onChange
}: {
  label: string;
  checked: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <input type="checkbox" className="h-4 w-4 rounded border-input" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      {label}
    </label>
  );
}

type IdentitySectionProps = {
  tenantId: string;
  name: string;
  enabled: boolean;
  onNameChange: (name: string) => void;
  onEnabledChange: (enabled: boolean) => void;
};

export function IdentitySection({ tenantId, name, enabled, onNameChange, onEnabledChange }: IdentitySectionProps) {
  return (
    <SectionFrame title="Identity" description="Tenant identity and activation state. Tenant ID is generated from name.">
      <div className="grid gap-3 md:grid-cols-2">
        <div className="space-y-2">
          <FieldLabel>Tenant ID</FieldLabel>
          <Input disabled value={tenantId} placeholder="generated from tenant name" />
        </div>
        <div className="space-y-2">
          <FieldLabel>Name</FieldLabel>
          <Input value={name} onChange={(event) => onNameChange(event.target.value)} placeholder="Tenant Demo" />
        </div>
        <Toggle label="Tenant enabled" checked={enabled} onChange={onEnabledChange} />
      </div>
    </SectionFrame>
  );
}

type JiraSectionProps = {
  jira: TenantFormValues["jira"];
  projectKeysText: string;
  readyStatusesText: string;
  onJiraChange: (jira: TenantFormValues["jira"]) => void;
  onProjectKeysTextChange: (value: string) => void;
  onReadyStatusesTextChange: (value: string) => void;
};

export function JiraSection({
  jira,
  projectKeysText,
  readyStatusesText,
  onJiraChange,
  onProjectKeysTextChange,
  onReadyStatusesTextChange
}: JiraSectionProps) {
  return (
    <SectionFrame title="Jira" description="Jira webhook and issue discovery configuration.">
      <div className="grid gap-3 md:grid-cols-2">
        <div className="space-y-2 md:col-span-2">
          <FieldLabel>OAuth Connection ID</FieldLabel>
          <Input value={jira.connection_id ?? ""} disabled placeholder="Connect Jira to generate a connection" />
        </div>
        <div className="space-y-2">
          <FieldLabel>Project keys (comma separated)</FieldLabel>
          <Input value={projectKeysText} onChange={(event) => onProjectKeysTextChange(event.target.value)} placeholder="TP, APP" />
        </div>
        <div className="space-y-2">
          <FieldLabel>Ready statuses (comma separated)</FieldLabel>
          <Input value={readyStatusesText} onChange={(event) => onReadyStatusesTextChange(event.target.value)} placeholder="Ready for Agent, Ready" />
        </div>
        <div className="space-y-2 md:col-span-2">
          <FieldLabel>Ready JQL (optional)</FieldLabel>
          <Textarea
            value={jira.ready_jql ?? ""}
            onChange={(event) => onJiraChange({ ...jira, ready_jql: event.target.value || null })}
            className="min-h-[88px]"
            placeholder='project in ("TP") AND status in ("Ready for Agent") ORDER BY updated DESC'
          />
        </div>
        <div className="space-y-2">
          <FieldLabel>Ready label</FieldLabel>
          <Input value={jira.ready_label} onChange={(event) => onJiraChange({ ...jira, ready_label: event.target.value })} placeholder="agent:ready" />
        </div>
        <div className="space-y-2">
          <FieldLabel>In progress label</FieldLabel>
          <Input
            value={jira.in_progress_label}
            onChange={(event) => onJiraChange({ ...jira, in_progress_label: event.target.value })}
            placeholder="agent:in-progress"
          />
        </div>
        <div className="space-y-2">
          <FieldLabel>Blocked label</FieldLabel>
          <Input value={jira.blocked_label} onChange={(event) => onJiraChange({ ...jira, blocked_label: event.target.value })} placeholder="agent:blocked" />
        </div>
        <div className="space-y-2">
          <FieldLabel>Done label (optional)</FieldLabel>
          <Input value={jira.done_label ?? ""} onChange={(event) => onJiraChange({ ...jira, done_label: event.target.value || null })} placeholder="agent:done" />
        </div>
        <div className="space-y-2 md:col-span-2">
          <FieldLabel>Webhook secret ref (optional)</FieldLabel>
          <Input
            value={jira.webhook_secret_ref ?? ""}
            onChange={(event) => onJiraChange({ ...jira, webhook_secret_ref: event.target.value || null })}
            placeholder="secret/jira-webhook"
          />
        </div>
      </div>
    </SectionFrame>
  );
}

type GitHubSectionProps = {
  github: TenantFormValues["github"];
  onGitHubChange: (github: TenantFormValues["github"]) => void;
};

export function GitHubSection({ github, onGitHubChange }: GitHubSectionProps) {
  return (
    <SectionFrame title="GitHub" description="GitHub App connection and webhook settings.">
      <div className="grid gap-3 md:grid-cols-2">
        <div className="space-y-2">
          <FieldLabel>Mode</FieldLabel>
          <Input value={github.mode} onChange={(event) => onGitHubChange({ ...github, mode: event.target.value })} placeholder="github_app" />
        </div>
        <div className="rounded-md border border-dashed bg-muted/40 px-3 py-2 text-xs text-muted-foreground md:col-span-2">
          Installation ID is set by the Connect GitHub App flow. App credentials are managed server-side.
        </div>
        <div className="space-y-2 md:col-span-2">
          <FieldLabel>Webhook secret ref (optional)</FieldLabel>
          <Input
            value={github.webhook_secret_ref ?? ""}
            onChange={(event) => onGitHubChange({ ...github, webhook_secret_ref: event.target.value || null })}
            placeholder="secret/github-webhook"
          />
        </div>
      </div>
    </SectionFrame>
  );
}

type RepositoryMappingSectionProps = {
  githubRepositoryText: string;
  repositoryOptions: GitHubRepositoryRecord[];
  repositoriesLoading: boolean;
  onGithubRepositoryTextChange: (value: string) => void;
};

export function RepositoryMappingSection({
  githubRepositoryText,
  repositoryOptions,
  repositoriesLoading,
  onGithubRepositoryTextChange
}: RepositoryMappingSectionProps) {
  return (
    <SectionFrame title="Repository Mapping" description="Define the target repository.">
      <div className="space-y-2">
        <FieldLabel>Repository</FieldLabel>
        <select
          className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
          value={githubRepositoryText}
          onChange={(event) => onGithubRepositoryTextChange(event.target.value)}
          disabled={repositoriesLoading || repositoryOptions.length === 0}
        >
          <option value="">
            {repositoriesLoading ? "Loading repositories..." : repositoryOptions.length === 0 ? "No repositories available" : "Select repository"}
          </option>
          {repositoryOptions.map((repo) => (
            <option key={repo.html_url} value={repo.html_url}>
              {repo.full_name}
            </option>
          ))}
        </select>
      </div>
    </SectionFrame>
  );
}

type PolicySectionProps = {
  policy: TenantFormValues["policy"];
  policyAllowedCommandsText: string;
  onPolicyChange: (policy: TenantFormValues["policy"]) => void;
  onPolicyAllowedCommandsTextChange: (value: string) => void;
};

export function PolicySection({
  policy,
  policyAllowedCommandsText,
  onPolicyChange,
  onPolicyAllowedCommandsTextChange
}: PolicySectionProps) {
  return (
    <SectionFrame title="Policy" description="Execution limits and safety policy.">
      <div className="space-y-3">
        <div className="grid gap-3 md:grid-cols-3">
          <div className="space-y-2">
            <FieldLabel>Max runtime (minutes)</FieldLabel>
            <Input type="number" value={String(policy.max_runtime_minutes)} onChange={(event) => onPolicyChange({ ...policy, max_runtime_minutes: Number(event.target.value || 0) })} />
          </div>
          <div className="space-y-2">
            <FieldLabel>Max loops</FieldLabel>
            <Input
              type="number"
              value={String(policy.max_dev_test_review_loops)}
              onChange={(event) => onPolicyChange({ ...policy, max_dev_test_review_loops: Number(event.target.value || 0) })}
            />
          </div>
          <div className="space-y-2">
            <FieldLabel>Max concurrent runs</FieldLabel>
            <Input type="number" value={String(policy.max_concurrent_runs)} onChange={(event) => onPolicyChange({ ...policy, max_concurrent_runs: Number(event.target.value || 0) })} />
          </div>
        </div>
        <div className="grid gap-2 md:grid-cols-2">
          <Toggle label="Allow Jira transitions" checked={policy.allow_jira_transitions} onChange={(next) => onPolicyChange({ ...policy, allow_jira_transitions: next })} />
          <Toggle label="Allow PR creation" checked={policy.allow_pr_creation} onChange={(next) => onPolicyChange({ ...policy, allow_pr_creation: next })} />
          <Toggle label="Allow label mutations" checked={policy.allow_label_mutations} onChange={(next) => onPolicyChange({ ...policy, allow_label_mutations: next })} />
          <Toggle label="Require AGENTS.md" checked={policy.require_agents_md} onChange={(next) => onPolicyChange({ ...policy, require_agents_md: next })} />
        </div>
        <div className="space-y-2">
          <FieldLabel>Allowed commands (one per line)</FieldLabel>
          <Textarea value={policyAllowedCommandsText} onChange={(event) => onPolicyAllowedCommandsTextChange(event.target.value)} placeholder="git status" className="min-h-[110px]" />
        </div>
      </div>
    </SectionFrame>
  );
}

type DiscordSectionProps = {
  discordEnabled: boolean;
  notifyEvents: string[];
  onDiscordEnabledChange: (enabled: boolean) => void;
  onToggleDiscordNotifyEvent: (eventValue: string, enabled: boolean) => void;
};

export function DiscordSection({
  discordEnabled,
  notifyEvents,
  onDiscordEnabledChange,
  onToggleDiscordNotifyEvent
}: DiscordSectionProps) {
  return (
    <SectionFrame title="Discord (optional)" description="Notification events for this tenant. Channel binding is managed automatically by the backend.">
      <div className="space-y-3">
        <Toggle label="Enable Discord settings" checked={discordEnabled} onChange={onDiscordEnabledChange} />
        {discordEnabled ? (
          <div className="space-y-2">
            <FieldLabel>Notify events</FieldLabel>
            <div className="space-y-2 rounded-md border p-3">
              {DISCORD_NOTIFY_EVENT_OPTIONS.map((option) => (
                <label key={option.value} className="flex items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    className="mt-0.5 h-4 w-4 rounded border-input"
                    checked={notifyEvents.includes(option.value)}
                    onChange={(event) => onToggleDiscordNotifyEvent(option.value, event.target.checked)}
                  />
                  <span>
                    <span className="font-medium text-foreground">{option.label}</span>
                    <span className="block text-xs text-muted-foreground">{option.description}</span>
                  </span>
                </label>
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </SectionFrame>
  );
}
