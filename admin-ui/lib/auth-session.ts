import type { AuthenticatedPrincipalRecord, MembershipRecord } from "@/lib/api";

const INVALID_SESSION = "Invalid authentication session. Sign in again.";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonemptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(isNonemptyString);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isMembership(value: unknown): value is MembershipRecord {
  return isRecord(value) &&
    isNonemptyString(value.membership_id) && isNonemptyString(value.tenant_id) &&
    isNonemptyString(value.role) && isStringArray(value.permission_keys) &&
    (value.effective_mode === "technical" || value.effective_mode === "non_technical") &&
    (value.mode_override === null || value.mode_override === "technical" || value.mode_override === "non_technical") &&
    (value.onboarding_kind === "tenant_admin_setup" || value.onboarding_kind === "member_join") &&
    isNullableString(value.first_signed_in_at) && isNullableString(value.onboarding_completed_at) &&
    isNullableString(value.onboarding_version) && isStringArray(value.team_ids) && isRecord(value.discord_state);
}

function isPrincipal(value: unknown): value is AuthenticatedPrincipalRecord {
  if (!isRecord(value) || !Array.isArray(value.memberships)) return false;
  for (const field of ["username", "user_id", "email", "full_name"]) {
    if (value[field] !== undefined && !isNullableString(value[field])) return false;
  }
  if (value.principal_type === "platform_super_admin") {
    return isNonemptyString(value.username) && value.memberships.length === 0;
  }
  return value.principal_type === "tenant_user" &&
    isNonemptyString(value.user_id) && isNonemptyString(value.email) &&
    value.memberships.length > 0 && value.memberships.every(isMembership);
}

// Both current credential producers supply identity and a backend bearer.
// Old or malformed cookies cannot be repaired by inferring a privileged role.
export function requireAuthSession(value: unknown): {
  accessToken: string;
  principal: AuthenticatedPrincipalRecord;
} {
  if (!isRecord(value) || !isNonemptyString(value.accessToken) || !isPrincipal(value.principal)) {
    throw new Error(INVALID_SESSION);
  }
  return { accessToken: value.accessToken, principal: value.principal };
}
