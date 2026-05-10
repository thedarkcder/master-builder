import type { TenantFormTextFields, TenantFormValues } from "@/lib/tenant-form";

export const WIZARD_DRAFT_KEY = "mb_tenant_wizard_draft_v1";

export type WizardDraft = {
  values: TenantFormValues;
  textFields: TenantFormTextFields;
  createdTenantId: string;
  selectedRepoUrl: string;
};

export function readWizardDraft(): Partial<WizardDraft> | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const raw = window.sessionStorage.getItem(WIZARD_DRAFT_KEY);
    if (!raw) {
      return null;
    }
    return JSON.parse(raw) as Partial<WizardDraft>;
  } catch {
    return null;
  }
}

export function persistWizardDraft(draft: WizardDraft): void {
  if (typeof window === "undefined") {
    return;
  }
  window.sessionStorage.setItem(WIZARD_DRAFT_KEY, JSON.stringify(draft));
}

export function clearWizardDraft(): void {
  if (typeof window === "undefined") {
    return;
  }
  window.sessionStorage.removeItem(WIZARD_DRAFT_KEY);
}

export function previewTenantId(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}
