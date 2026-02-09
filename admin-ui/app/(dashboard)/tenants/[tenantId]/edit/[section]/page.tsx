import { notFound, redirect } from "next/navigation";

import { TenantEditPage } from "@/components/tenant-edit-page";

const ALLOWED = ["setup", "integrations", "jira", "github", "discord", "health", "config", "webhooks", "access"] as const;
type Section = (typeof ALLOWED)[number];

export default async function TenantEditSectionPage({ params }: { params: Promise<{ tenantId: string; section: string }> }) {
  const { tenantId, section } = await params;
  if (section === "webhooks") {
    redirect(`/tenants/${encodeURIComponent(tenantId)}/edit/jira`);
  }
  if (!ALLOWED.includes(section as Section)) {
    notFound();
  }
  return <TenantEditPage section={section as Exclude<Section, "webhooks">} />;
}
