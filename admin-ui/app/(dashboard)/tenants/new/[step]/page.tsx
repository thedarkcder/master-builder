import { notFound } from "next/navigation";

import { TenantSetupWizard, type WizardStepKey } from "@/components/tenant-setup-wizard";

const STEP_KEYS: WizardStepKey[] = ["basics", "jira", "github", "discord", "repos", "review"];

export default async function NewTenantStepPage({ params }: { params: Promise<{ step: string }> }) {
  const { step } = await params;
  if (!STEP_KEYS.includes(step as WizardStepKey)) {
    notFound();
  }

  return <TenantSetupWizard stepKey={step as WizardStepKey} />;
}
