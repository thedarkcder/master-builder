import type { AuthenticatedPrincipalRecord, MembershipRecord } from "@/lib/api";

type PrincipalLike = Pick<AuthenticatedPrincipalRecord, "principal_type" | "memberships">;

export function getPlatformAdminHomeRoute(): string {
  return "/platform/dashboard";
}

export function getPlatformStatusRoute(): string {
  return "/platform/status";
}

export function getWorkspaceSelectorRoute(): string {
  return "/tenants/select";
}

export function getTenantWorkspaceRoute(tenantId: string): string {
  return `/${encodeURIComponent(tenantId)}`;
}

export function getTenantDashboardRoute(tenantId: string): string {
  return `${getTenantWorkspaceRoute(tenantId)}/dashboard`;
}

export function getTenantSettingsRoute(tenantId: string, section = "integrations"): string {
  return `${getTenantWorkspaceRoute(tenantId)}/settings/${section}`;
}

export function getTenantSetupRoute(tenantId: string): string {
  return `/tenants/new/basics?tenant_id=${encodeURIComponent(tenantId)}`;
}

export function getMembershipForTenant(
  principal: PrincipalLike | null | undefined,
  tenantId: string,
): MembershipRecord | null {
  if (!principal || principal.principal_type === "platform_super_admin") {
    return null;
  }
  return principal.memberships.find((membership) => membership.tenant_id === tenantId) ?? null;
}

export function canAccessPlatformAdmin(principal: PrincipalLike | null | undefined): boolean {
  return principal?.principal_type === "platform_super_admin";
}

export function canAccessTenantWorkspace(
  principal: PrincipalLike | null | undefined,
  tenantId: string,
): boolean {
  return canAccessPlatformAdmin(principal) || Boolean(getMembershipForTenant(principal, tenantId));
}

export function canManageTeam(
  principal: PrincipalLike | null | undefined,
  tenantId: string,
): boolean {
  if (canAccessPlatformAdmin(principal)) {
    return true;
  }
  const membership = getMembershipForTenant(principal, tenantId);
  return Boolean(membership?.permission_keys.includes("people.manage"));
}

export function canManageProjects(
  principal: PrincipalLike | null | undefined,
  tenantId: string,
): boolean {
  if (canAccessPlatformAdmin(principal)) {
    return true;
  }
  const membership = getMembershipForTenant(principal, tenantId);
  return Boolean(
    membership?.permission_keys.includes("projects.manage") ||
      membership?.permission_keys.includes("workspace.manage"),
  );
}

export function getProjectArchiveRedirectRoute(
  principal: PrincipalLike | null | undefined,
  tenantId: string,
): string {
  if (canAccessPlatformAdmin(principal)) {
    return getWorkspaceSelectorRoute();
  }
  return getTenantSetupRoute(tenantId);
}

export function getTenantArchiveRedirectRoute(
  principal: PrincipalLike | null | undefined,
  tenantId: string,
): string {
  if (canAccessPlatformAdmin(principal)) {
    return getWorkspaceSelectorRoute();
  }
  return getTenantSetupRoute(tenantId);
}

export function getTenantArchiveConfirmationRoute(
  principal: PrincipalLike | null | undefined,
  tenantId: string,
  options?: { purgeAfterAt?: string | null },
): string {
  const destination = canAccessPlatformAdmin(principal) ? "selector" : "setup";
  const params = new URLSearchParams({
    destination,
    next: getTenantArchiveRedirectRoute(principal, tenantId),
  });
  if (options?.purgeAfterAt) {
    params.set("purge_after", options.purgeAfterAt);
  }
  return `${getTenantWorkspaceRoute(tenantId)}/archived?${params.toString()}`;
}

export function canAccessTechnicalSurface(
  principal: PrincipalLike | null | undefined,
  tenantId: string,
): boolean {
  if (canAccessPlatformAdmin(principal)) {
    return true;
  }
  const membership = getMembershipForTenant(principal, tenantId);
  return Boolean(membership?.permission_keys.includes("technical.access")) && membership?.effective_mode === "technical";
}

function getPendingMembership(
  principal: PrincipalLike,
  preferredTenantId?: string | null,
): MembershipRecord | null {
  if (principal.principal_type === "platform_super_admin") {
    return null;
  }
  const memberships = principal.memberships ?? [];
  if (preferredTenantId) {
    const preferredPending = memberships.find(
      (membership) =>
        membership.tenant_id === preferredTenantId && membership.onboarding_completed_at == null,
    );
    if (preferredPending) {
      return preferredPending;
    }
  }
  return memberships.find((membership) => membership.onboarding_completed_at == null) ?? null;
}

export function getDefaultAuthenticatedRoute(
  principal: PrincipalLike | null | undefined,
  options?: { preferredTenantId?: string | null },
): string {
  if (!principal) {
    return "/login";
  }
  if (principal.principal_type === "platform_super_admin") {
    return getPlatformAdminHomeRoute();
  }

  const pendingMembership = getPendingMembership(principal, options?.preferredTenantId);
  if (pendingMembership) {
    return "/get-started";
  }

  const memberships = principal.memberships ?? [];
  if (options?.preferredTenantId) {
    const preferredMembership = memberships.find(
      (membership) => membership.tenant_id === options.preferredTenantId,
    );
    if (preferredMembership) {
      return getTenantDashboardRoute(preferredMembership.tenant_id);
    }
  }

  if (memberships.length === 1) {
    return getTenantDashboardRoute(memberships[0].tenant_id);
  }
  if (memberships.length > 1) {
    return getWorkspaceSelectorRoute();
  }
  return "/login";
}
