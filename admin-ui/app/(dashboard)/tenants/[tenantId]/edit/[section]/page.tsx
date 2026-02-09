import { notFound } from "next/navigation";

import { TenantEditPage } from "@/components/tenant-edit-page";

const ALLOWED = ["setup", "integrations", "jira", "github", "discord", "health", "config", "webhooks", "access"] as const;
type Section = (typeof ALLOWED)[number];

export default async function TenantEditSectionPage({ params }: { params: Promise<{ section: string }> }) {
  const { section } = await params;
  if (!ALLOWED.includes(section as Section)) {
    notFound();
  }
  return <TenantEditPage section={section as Section} />;
}
