import { notFound, redirect } from "next/navigation";

import { TenantEditPage } from "@/components/tenant-edit-page";

const ALLOWED = [
  "setup",
  "integrations",
  "jira",
  "github",
  "discord",
  "health",
  "config",
  "secrets",
  "projects",
  "webhooks",
  "access",
  "notifications"
] as const;
type Section = (typeof ALLOWED)[number];

export default async function TenantEditSectionPage({ params }: { params: Promise<{ tenantId: string; section: string }> }) {
  const { tenantId, section } = await params;
  if (section === "webhooks") {
    redirect(`/tenants/${encodeURIComponent(tenantId)}/edit/jira`);
  }
  if (section === "projects") {
    redirect(`/tenants/${encodeURIComponent(tenantId)}/projects`);
  }
  if (section === "secrets") {
    redirect(`/tenants/${encodeURIComponent(tenantId)}/secrets`);
  }
  if (section === "access") {
    redirect(`/tenants/${encodeURIComponent(tenantId)}/team/members`);
  }
  if (!ALLOWED.includes(section as Section)) {
    notFound();
  }
  return <TenantEditPage section={section as Exclude<Section, "webhooks" | "access" | "secrets">} />;
}
