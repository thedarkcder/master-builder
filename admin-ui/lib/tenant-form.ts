import type {
  DiscordConfig,
  GithubConfig,
  JiraConfig,
  PolicyConfig,
  ReposConfig,
  TenantCreatePayload,
  TenantRecord,
  TenantUpdatePayload
} from "@/lib/api";

export type TenantFormValues = {
  tenantId: string;
  name: string;
  isEnabled: boolean;
  jira: JiraConfig;
  github: GithubConfig;
  repos: ReposConfig;
  policy: PolicyConfig;
  discordEnabled: boolean;
  discord: DiscordConfig;
};

export function splitCsv(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function joinCsv(values: string[]): string {
  return values.join(", ");
}

function parseMultiLine(value: string): string[] {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

export function defaultTenantFormValues(): TenantFormValues {
  return {
    tenantId: "",
    name: "",
    isEnabled: true,
    jira: {
      connection_id: null,
      project_keys: [],
      ready_statuses: ["Ready for Agent"],
      ready_jql: null,
      ready_label: "agent:ready",
      in_progress_label: "agent:in-progress",
      blocked_label: "agent:blocked",
      done_label: "agent:done",
      webhook_secret_ref: null
    },
    github: {
      mode: "github_app",
      webhook_secret_ref: null,
      installation_id: null
    },
    repos: {
      github_repository: null
    },
    policy: {
      allow_jira_transitions: false,
      allow_pr_creation: true,
      allow_label_mutations: true,
      max_runtime_minutes: 30,
      max_dev_test_review_loops: 2,
      max_concurrent_runs: 2,
      allowed_commands: [],
      require_agents_md: false
    },
    discordEnabled: false,
    discord: {
      channel_id: null,
      channel_name_template: "proj-{tenant_id}",
      notify_events: []
    }
  };
}

export function recordToFormValues(record: TenantRecord): TenantFormValues {
  return {
    tenantId: record.tenant_id,
    name: record.name,
    isEnabled: record.is_enabled,
    jira: record.jira,
    github: record.github,
    repos: record.repos,
    policy: record.policy,
    discordEnabled: Boolean(record.discord),
    discord:
      record.discord ?? {
        channel_id: null,
        channel_name_template: "proj-{tenant_id}",
        notify_events: []
      }
  };
}

export type TenantFormTextFields = {
  projectKeysText: string;
  readyStatusesText: string;
  githubRepositoryText: string;
  policyAllowedCommandsText: string;
};

export function formValuesToTextFields(values: TenantFormValues): TenantFormTextFields {
  return {
    projectKeysText: joinCsv(values.jira.project_keys),
    readyStatusesText: joinCsv(values.jira.ready_statuses),
    githubRepositoryText: values.repos.github_repository ?? "",
    policyAllowedCommandsText: values.policy.allowed_commands.join("\n")
  };
}

export function toCreatePayload(
  values: TenantFormValues,
  textFields: TenantFormTextFields
): TenantCreatePayload {
  const githubRepository = textFields.githubRepositoryText.trim();
  const projectKeys = splitCsv(textFields.projectKeysText);

  return {
    name: values.name.trim(),
    is_enabled: values.isEnabled,
    jira: {
      ...values.jira,
      connection_id: values.jira.connection_id?.trim() || null,
      project_keys: projectKeys,
      ready_statuses: splitCsv(textFields.readyStatusesText),
      ready_jql: values.jira.ready_jql?.trim() || null,
      ready_label: values.jira.ready_label.trim(),
      in_progress_label: values.jira.in_progress_label.trim(),
      blocked_label: values.jira.blocked_label.trim(),
      done_label: values.jira.done_label?.trim() || null,
      webhook_secret_ref: values.jira.webhook_secret_ref?.trim() || null
    },
    github: {
      ...values.github,
      mode: values.github.mode.trim(),
      webhook_secret_ref: values.github.webhook_secret_ref?.trim() || null,
      installation_id: values.github.installation_id?.trim() || null
    },
    repos: {
      github_repository: githubRepository || null
    },
    policy: {
      ...values.policy,
      allowed_commands: parseMultiLine(textFields.policyAllowedCommandsText),
      max_runtime_minutes: Number(values.policy.max_runtime_minutes),
      max_dev_test_review_loops: Number(values.policy.max_dev_test_review_loops),
      max_concurrent_runs: Number(values.policy.max_concurrent_runs)
    },
    discord: values.discordEnabled
      ? {
          channel_id: values.discord.channel_id?.trim() || null,
          channel_name_template: values.discord.channel_name_template.trim() || "proj-{tenant_id}",
          notify_events: values.discord.notify_events
            .map((value) => value.trim())
            .filter((value, index, array) => value.length > 0 && array.indexOf(value) === index)
        }
      : null
  };
}

export function toUpdatePayload(
  values: TenantFormValues,
  textFields: TenantFormTextFields
): TenantUpdatePayload {
  const created = toCreatePayload(values, textFields);
  return {
    name: created.name,
    is_enabled: created.is_enabled,
    jira: created.jira,
    github: created.github,
    repos: created.repos,
    policy: created.policy,
    discord: created.discord
  };
}
