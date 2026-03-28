import type { AuthenticatedPrincipalRecord } from "@/lib/api";

export function getTenantDashboardRoute(tenantId: string): string {
  return `/tenants/${encodeURIComponent(tenantId)}/dashboard`;
}

export function getDefaultAuthenticatedRoute(
  principal: AuthenticatedPrincipalRecord | null | undefined
): string {
  if (!principal) {
    return "/login";
  }
  if (principal.principal_type === "platform_super_admin") {
    return "/tenants/select";
  }
  const primaryMembership = principal.memberships[0];
  if (!primaryMembership) {
    return "/tenants/select";
  }
  return getTenantDashboardRoute(primaryMembership.tenant_id);
}
