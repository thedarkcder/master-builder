import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { getDefaultAuthenticatedRoute } from "@/lib/auth-routing";
import { getLastWorkspaceCookieName } from "@/lib/workspace-preference";

export default async function DashboardEntryPage() {
  const session = await auth();
  if (!session) redirect("/login");
  const principal = session.user?.principal;
  if (!principal) throw new Error("Authenticated session is missing its principal");
  const cookieStore = await cookies();
  const preferredTenantId = cookieStore.get(getLastWorkspaceCookieName())?.value ?? null;
  redirect(getDefaultAuthenticatedRoute(principal, { preferredTenantId }));
}
