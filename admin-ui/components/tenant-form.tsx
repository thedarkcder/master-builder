"use client";

import type { CodexModelOptionRecord, GitHubRepositoryRecord, TenantCreatePayload } from "@/lib/api";
import { TenantFormContent } from "@/components/tenant-form-content";
import type { TenantFormVisibleSections } from "@/components/tenant-form-config";
import { useTenantFormState } from "@/components/use-tenant-form-state";
import { type TenantFormPayload, type TenantFormValues, toCreatePayload, toUpdatePayload } from "@/lib/tenant-form";

type TenantFormProps = {
  submitting?: boolean;
  repositoryOptions?: GitHubRepositoryRecord[];
  codexModels?: CodexModelOptionRecord[];
  reasoningEfforts?: CodexModelOptionRecord[];
  globalCodexModel?: string;
  globalCodexReasoningEffort?: string;
  repositoriesLoading?: boolean;
  onRefreshRepositoryOptions?: () => void;
  visibleSections?: TenantFormVisibleSections;
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
  const submitLabel = mode === "create" ? "Create Tenant" : "Save Tenant";
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
      onRefreshRepositoryOptions={onRefreshRepositoryOptions}
      onSubmit={() => void handleSubmit()}
    />
  );
}
