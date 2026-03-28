import { Suspense } from "react";
import { notFound } from "next/navigation";

import { TenantSetupWizard, type WizardStepKey } from "@/components/tenant-setup-wizard";

const STEP_KEYS: WizardStepKey[] = ["basics", "jira", "github", "discord", "repos", "review"];

export default async function NewTenantStepPage({ params }: { params: Promise<{ step: string }> }) {
  const { step } = await params;
  if (!STEP_KEYS.includes(step as WizardStepKey)) {
    notFound();
  }

  return (
    <Suspense fallback={<div className="p-8 text-muted-foreground">Loading…</div>}>
      <TenantSetupWizard stepKey={step as WizardStepKey} />
    </Suspense>
  );
}
