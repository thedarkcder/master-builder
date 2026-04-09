import { Suspense } from "react";
import { notFound } from "next/navigation";

import { TenantSetupWizard } from "@/components/tenant-setup-wizard";
import { isWizardStepKey } from "@/components/tenant-setup/types";

export default async function NewTenantStepPage({ params }: { params: Promise<{ step: string }> }) {
  const { step } = await params;
  if (!isWizardStepKey(step)) {
    notFound();
  }

  return (
    <Suspense fallback={<div className="p-8 text-muted-foreground">Loading…</div>}>
      <TenantSetupWizard stepKey={step} />
    </Suspense>
  );
}
