import "next-auth";
import "next-auth/jwt";

type PrincipalShape = {
  principal_type: "platform_super_admin" | "tenant_user";
  username?: string | null;
  user_id?: string | null;
  email?: string | null;
  full_name?: string | null;
  memberships: Array<{
    membership_id: string;
    tenant_id: string;
    role: string;
    permission_keys: string[];
    effective_mode: "technical" | "non_technical";
    mode_override: "technical" | "non_technical" | null;
    onboarding_kind: "tenant_admin_setup" | "member_join";
    first_signed_in_at: string | null;
    onboarding_completed_at: string | null;
    onboarding_version: string | null;
    team_ids: string[];
    discord_state: Record<string, unknown>;
  }>;
};

declare module "next-auth" {
  interface User {
    accessToken?: string;
    principal?: PrincipalShape;
  }

  interface Session {
    user: {
      name?: string | null;
      email?: string | null;
      image?: string | null;
      principal: PrincipalShape;
    };
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    accessToken?: string;
    principal?: PrincipalShape;
  }
}
