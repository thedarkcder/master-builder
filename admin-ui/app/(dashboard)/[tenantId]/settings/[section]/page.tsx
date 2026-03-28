import { notFound } from "next/navigation";

import { TenantEditPage } from "@/components/tenant-edit-page";

const ALLOWED = [
  "setup",
  "integrations",
  "jira",
  "github",
  "discord",
  "health",
  "config",
  "notifications",
] as const;
type Section = (typeof ALLOWED)[number];

export default async function TenantSettingsSectionPage({
  params,
}: {
  params: Promise<{ tenantId: string; section: string }>;
}) {
  const { section } = await params;
  if (!ALLOWED.includes(section as Section)) {
    notFound();
  }
  return <TenantEditPage section={section as Section} />;
}
