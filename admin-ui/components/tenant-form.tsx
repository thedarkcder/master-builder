"use client";

import { useEffect, useMemo, useState } from "react";

import type { CodexModelOptionRecord, GitHubRepositoryRecord, TenantCreatePayload } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  DiscordSection,
  GitHubSection,
  IdentitySection,
  JiraSection,
  PolicySection,
  RepositoryMappingSection
} from "@/components/tenant-form-sections";
import {
  defaultTenantFormValues,
  formValuesToTextFields,
  type TenantFormPayload,
  type TenantFormValues,
  toCreatePayload,
  toUpdatePayload
} from "@/lib/tenant-form";

type TenantFormProps = {
  submitting?: boolean;
  repositoryOptions?: GitHubRepositoryRecord[];
  codexModels?: CodexModelOptionRecord[];
  reasoningEfforts?: CodexModelOptionRecord[];
  globalCodexModel?: string;
  globalCodexReasoningEffort?: string;
  repositoriesLoading?: boolean;
  onRefreshRepositoryOptions?: () => void;
  visibleSections?: {
    identity?: boolean;
    jira?: boolean;
    github?: boolean;
    repository?: boolean;
    policy?: boolean;
    discord?: boolean;
  };
} & (
  | {
      mode: "create";
      initialValues?: TenantFormValues;
      onSubmit: (payload: TenantCreatePayload) => Promise<void>;
    }
  | {
      mode: "edit";
      initialValues: TenantFormValues;
      onSubmit: (payload: TenantFormPayload) => Promise<void>;
    }
);

export function TenantForm({
  mode,
  initialValues,
  onSubmit,
  submitting = false,
  repositoryOptions = [],
  codexModels = [],
  reasoningEfforts = [],
  globalCodexModel = "",
  globalCodexReasoningEffort = "",
  repositoriesLoading = false,
  onRefreshRepositoryOptions,
  visibleSections
}: TenantFormProps) {
  const [values, setValues] = useState<TenantFormValues>(initialValues ?? defaultTenantFormValues());
  const [error, setError] = useState("");
  const [textFields, setTextFields] = useState(() => formValuesToTextFields(initialValues ?? defaultTenantFormValues()));

  useEffect(() => {
    const nextValues = initialValues ?? defaultTenantFormValues();
    setValues(nextValues);
    setTextFields(formValuesToTextFields(nextValues));
  }, [initialValues]);

  const submitLabel = mode === "create" ? "Create Tenant" : "Save Tenant";
  const sections = {
    identity: visibleSections?.identity ?? true,
    jira: visibleSections?.jira ?? true,
    github: visibleSections?.github ?? true,
    repository: visibleSections?.repository ?? true,
    policy: visibleSections?.policy ?? true,
    discord: visibleSections?.discord ?? true
  };

  function toggleDiscordNotifyEvent(eventValue: string, enabled: boolean): void {
    setValues((prev) => {
      const current = prev.discord.notify_events;
      const next = enabled ? Array.from(new Set([...current, eventValue])) : current.filter((value) => value !== eventValue);
      return {
        ...prev,
        discord: { ...prev.discord, notify_events: next }
      };
    });
  }

  const validation = useMemo(() => {
    if (sections.identity && !values.name.trim()) {
      return "Tenant name is required.";
    }
    if (sections.jira && !values.jira.connection_id?.trim()) {
      return "Connect Atlassian before saving.";
    }
    if (sections.jira && !textFields.projectKeysText.trim()) {
      return "At least one Jira project key is required.";
    }
    if (sections.jira && !textFields.readyStatusesText.trim()) {
      return "At least one ready status is required.";
    }
    if (sections.repository && !textFields.githubRepositoryText.trim()) {
      return "Repository selection is required.";
    }
    return "";
  }, [sections.identity, sections.jira, sections.repository, textFields.githubRepositoryText, textFields.projectKeysText, textFields.readyStatusesText, values]);

  async function handleSubmit(): Promise<void> {
    if (validation) {
      setError(validation);
      return;
    }

    setError("");
    if (mode === "create") {
      await onSubmit(toCreatePayload(values, textFields));
      return;
    }

    await onSubmit(toUpdatePayload(values, textFields));
  }

  return (
    <div className="space-y-4">
      {error ? <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p> : null}

      {sections.identity ? (
        <IdentitySection
          tenantId={values.tenantId}
          name={values.name}
          enabled={values.isEnabled}
          onNameChange={(name) => setValues((prev) => ({ ...prev, name }))}
          onEnabledChange={(enabled) => setValues((prev) => ({ ...prev, isEnabled: enabled }))}
        />
      ) : null}

      {sections.jira ? (
        <JiraSection
          jira={values.jira}
          projectKeysText={textFields.projectKeysText}
          readyStatusesText={textFields.readyStatusesText}
          onJiraChange={(jira) => setValues((prev) => ({ ...prev, jira }))}
          onProjectKeysTextChange={(value) => setTextFields((prev) => ({ ...prev, projectKeysText: value }))}
          onReadyStatusesTextChange={(value) => setTextFields((prev) => ({ ...prev, readyStatusesText: value }))}
        />
      ) : null}

      {sections.github ? (
        <GitHubSection github={values.github} onGitHubChange={(github) => setValues((prev) => ({ ...prev, github }))} />
      ) : null}

      {sections.repository ? (
        <RepositoryMappingSection
          githubRepositoryText={textFields.githubRepositoryText}
          repositoryOptions={repositoryOptions}
          repositoriesLoading={repositoriesLoading}
          onGithubRepositoryTextChange={(value) => setTextFields((prev) => ({ ...prev, githubRepositoryText: value }))}
          onRefreshRepositoryOptions={onRefreshRepositoryOptions}
        />
      ) : null}

      {sections.policy ? (
        <PolicySection
          policy={values.policy}
          policyAllowedCommandsText={textFields.policyAllowedCommandsText}
          codexModels={codexModels}
          reasoningEfforts={reasoningEfforts}
          globalCodexModel={globalCodexModel}
          globalCodexReasoningEffort={globalCodexReasoningEffort}
          onPolicyChange={(policy) => setValues((prev) => ({ ...prev, policy }))}
          onPolicyAllowedCommandsTextChange={(value) => setTextFields((prev) => ({ ...prev, policyAllowedCommandsText: value }))}
        />
      ) : null}

      {sections.discord ? (
        <DiscordSection
          discordEnabled={values.discordEnabled}
          notifyEvents={values.discord.notify_events}
          onDiscordEnabledChange={(enabled) => setValues((prev) => ({ ...prev, discordEnabled: enabled }))}
          onToggleDiscordNotifyEvent={toggleDiscordNotifyEvent}
        />
      ) : null}

      <div className="flex justify-end">
        <Button onClick={handleSubmit} disabled={submitting}>
          {submitting ? "Saving..." : submitLabel}
        </Button>
      </div>
    </div>
  );
}
