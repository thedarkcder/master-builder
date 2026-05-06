import type { TenantFormTextFields, TenantFormValues } from "@/lib/tenant-form";

export type TenantFormVisibleSections = {
  identity?: boolean;
  jira?: boolean;
  github?: boolean;
  repository?: boolean;
  policy?: boolean;
  discord?: boolean;
};

export type TenantFormSections = {
  identity: boolean;
  jira: boolean;
  github: boolean;
  repository: boolean;
  policy: boolean;
  discord: boolean;
};

export function resolveTenantFormSections(visibleSections?: TenantFormVisibleSections): TenantFormSections {
  return {
    identity: visibleSections?.identity ?? true,
    jira: visibleSections?.jira ?? true,
    github: visibleSections?.github ?? true,
    repository: visibleSections?.repository ?? true,
    policy: visibleSections?.policy ?? true,
    discord: visibleSections?.discord ?? true
  };
}

export function validateTenantForm({
  sections,
  values,
  textFields
}: {
  sections: TenantFormSections;
  values: TenantFormValues;
  textFields: TenantFormTextFields;
}): string {
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
}
