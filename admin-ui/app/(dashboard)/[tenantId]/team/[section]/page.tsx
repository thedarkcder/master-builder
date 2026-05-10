import { notFound } from "next/navigation";

import { TenantTeamPage } from "@/components/tenant-team-page";

const ALLOWED = ["members", "teams", "invites"] as const;
type AllowedTeamSection = (typeof ALLOWED)[number];

export default async function TenantTeamSectionPage({ params }: { params: Promise<{ tenantId: string; section: string }> }) {
  const { section } = await params;
  if (!ALLOWED.includes(section as AllowedTeamSection)) {
    notFound();
  }
  return <TenantTeamPage section={section as AllowedTeamSection} />;
}
