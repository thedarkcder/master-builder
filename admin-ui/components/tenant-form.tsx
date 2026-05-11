"use client";

import type {
  CodexModelOptionRecord,
  GitHubRepositoryRecord,
  TenantConfigurationUpdatePayload,
  TenantCreatePayload,
  TenantGithubUpdatePayload,
  TenantPolicyUpdatePayload
} from "@/lib/api";
import { TenantFormContent } from "@/components/tenant-form-content";
import type { TenantFormVisibleSections } from "@/components/tenant-form-config";
import { useTenantFormState } from "@/components/use-tenant-form-state";
import {
  type TenantFormPayload,
  type TenantFormValues,
  toConfigurationUpdatePayload,
  toCreatePayload,
  toGithubUpdatePayload,
  toPolicyUpdatePayload,
  toUpdatePayload
} from "@/lib/tenant-form";

type TenantEditSubmitScope = "full" | "configuration" | "github" | "policy";

type TenantEditSubmitPayloadByScope = {
  full: TenantFormPayload;
  configuration: TenantConfigurationUpdatePayload;
  github: TenantGithubUpdatePayload;
  policy: TenantPolicyUpdatePayload;
};

type TenantFormProps = {
  submitting?: boolean;
  repositoryOptions?: GitHubRepositoryRecord[];
  codexModels?: CodexModelOptionRecord[];
  reasoningEfforts?: CodexModelOptionRecord[];
  globalCodexModel?: string;
  globalCodexReasoningEffort?: string;
  repositoriesLoading?: boolean;
  visibleSections?: TenantFormVisibleSections;
  submitLabel?: string;
  submitScope?: TenantEditSubmitScope;
} & (
  | {
      mode: "create";
      initialValues?: TenantFormValues;
      onSubmit: (payload: TenantCreatePayload) => Promise<void>;
    }
  | {
      mode: "edit";
      initialValues: TenantFormValues;
      submitScope: "configuration";
      onSubmit: (payload: TenantEditSubmitPayloadByScope["configuration"]) => Promise<void>;
    }
  | {
      mode: "edit";
      initialValues: TenantFormValues;
      submitScope: "github";
      onSubmit: (payload: TenantEditSubmitPayloadByScope["github"]) => Promise<void>;
    }
  | {
      mode: "edit";
      initialValues: TenantFormValues;
      submitScope: "policy";
      onSubmit: (payload: TenantEditSubmitPayloadByScope["policy"]) => Promise<void>;
    }
  | {
      mode: "edit";
      initialValues: TenantFormValues;
      submitScope?: "full";
      onSubmit: (payload: TenantEditSubmitPayloadByScope["full"]) => Promise<void>;
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
  visibleSections,
  submitLabel: submitLabelOverride,
  submitScope
}: TenantFormProps) {
  const submitLabel = submitLabelOverride ?? (mode === "create" ? "Create Tenant" : "Save Tenant");
  const form = useTenantFormState({ initialValues, visibleSections });

  async function handleSubmit(): Promise<void> {
    if (form.validation) {
      form.setError(form.validation);
      return;
    }

    form.setError("");
    if (mode === "create") {
      await onSubmit(toCreatePayload(form.values, form.textFields));
      return;
    }

    if (submitScope === "configuration") {
      await onSubmit(toConfigurationUpdatePayload(form.values));
      return;
    }
    if (submitScope === "github") {
      await onSubmit(toGithubUpdatePayload(form.values));
      return;
    }
    if (submitScope === "policy") {
      await onSubmit(toPolicyUpdatePayload(form.values, form.textFields));
      return;
    }

    await onSubmit(toUpdatePayload(form.values, form.textFields));
  }

  return (
    <TenantFormContent
      values={form.values}
      setValues={form.setValues}
      textFields={form.textFields}
      setTextFields={form.setTextFields}
      sections={form.sections}
      error={form.error}
      submitting={submitting}
      submitLabel={submitLabel}
      repositoryOptions={repositoryOptions}
      codexModels={codexModels}
      reasoningEfforts={reasoningEfforts}
      globalCodexModel={globalCodexModel}
      globalCodexReasoningEffort={globalCodexReasoningEffort}
      repositoriesLoading={repositoriesLoading}
      onSubmit={() => void handleSubmit()}
    />
  );
}
