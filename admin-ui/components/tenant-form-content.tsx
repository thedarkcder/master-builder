"use client";

import type { Dispatch, SetStateAction } from "react";

import type { CodexModelOptionRecord, GitHubRepositoryRecord } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  DiscordSection,
  GitHubSection,
  IdentitySection,
  JiraSection,
  PolicySection,
  RepositoryMappingSection
} from "@/components/tenant-form-sections";
import type { TenantFormSections } from "@/components/tenant-form-config";
import type { TenantFormTextFields, TenantFormValues } from "@/lib/tenant-form";

type TenantFormContentProps = {
  values: TenantFormValues;
  setValues: Dispatch<SetStateAction<TenantFormValues>>;
  textFields: TenantFormTextFields;
  setTextFields: Dispatch<SetStateAction<TenantFormTextFields>>;
  sections: TenantFormSections;
  error: string;
  submitting: boolean;
  submitLabel: string;
  repositoryOptions: GitHubRepositoryRecord[];
  codexModels: CodexModelOptionRecord[];
  reasoningEfforts: CodexModelOptionRecord[];
  globalCodexModel: string;
  globalCodexReasoningEffort: string;
  repositoriesLoading: boolean;
  onSubmit: () => void;
};

export function TenantFormContent({
  values,
  setValues,
  textFields,
  setTextFields,
  sections,
  error,
  submitting,
  submitLabel,
  repositoryOptions,
  codexModels,
  reasoningEfforts,
  globalCodexModel,
  globalCodexReasoningEffort,
  repositoriesLoading,
  onSubmit
}: TenantFormContentProps) {
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

  return (
    <div className="space-y-4">
      {error ? <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p> : null}

      {sections.identity ? (
        <IdentitySection
          tenantId={values.tenantId}
          name={values.name}
          onNameChange={(name) => setValues((prev) => ({ ...prev, name }))}
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
        <Button onClick={onSubmit} disabled={submitting}>
          {submitting ? "Saving..." : submitLabel}
        </Button>
      </div>
    </div>
  );
}
