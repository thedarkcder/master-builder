"use client";

import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { Check, Shield, UserRound } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { DiscordLogo } from "@/components/icons/discord-logo";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { canAccessTenantWorkspace } from "@/lib/auth-routing";
import {
  changeTenantUserPassword,
  createTenantDiscordInvite,
  getTenant,
  getTenantDiscordIdentity,
  startTenantDiscordLink,
  updateAuthenticatedUserProfile,
  updateTenantUserSettings,
  type TenantDiscordIdentityRecord,
  type TenantRecord,
} from "@/lib/api";

export function TenantProfilePage({ section = "profile" }: { section?: "profile" | "security" }) {
  const params = useParams<{ tenantId: string }>();
  const { credentials, principal, ready, refreshPrincipal } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);
  const isTenantUser = principal?.principal_type === "tenant_user";
  const isPlatformSuperAdmin = principal?.principal_type === "platform_super_admin";
  const canEditAccountProfile = isTenantUser;
  const profileIdentifier = principal?.email ?? principal?.username ?? "";

  const membership = useMemo(
    () => principal?.memberships.find((item) => item.tenant_id === tenantId) ?? null,
    [principal, tenantId],
  );

  const [tenant, setTenant] = useState<TenantRecord | null>(null);
  const [discordIdentity, setDiscordIdentity] = useState<TenantDiscordIdentityRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [fullName, setFullName] = useState("");
  const [modeOverride, setModeOverride] = useState<"technical" | "non_technical" | "">("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [nextPassword, setNextPassword] = useState("");

  const displayName = fullName || principal?.full_name || principal?.username || principal?.email || "User";

  const statusClasses = useMemo(() => {
    const normalized = statusLine.toLowerCase();
    if (normalized.includes("failed") || normalized.includes("unable") || normalized.includes("incorrect")) {
      return "border-red-300 bg-red-50 text-red-800";
    }
    if (normalized.includes("updated") || normalized.includes("generated") || normalized.includes("copied")) {
      return "border-emerald-300 bg-emerald-50 text-emerald-800";
    }
    return "border-muted bg-muted/30 text-muted-foreground";
  }, [statusLine]);

  useEffect(() => {
    if (!membership) {
      setFullName(principal?.full_name ?? principal?.username ?? "");
      setModeOverride("");
      return;
    }
    setFullName(principal?.full_name ?? principal?.username ?? "");
    setModeOverride((membership.mode_override ?? "") as "technical" | "non_technical" | "");
  }, [membership, principal?.full_name, principal?.username]);

  useEffect(() => {
    if (!ready || !credentials || !canAccessTenantWorkspace(principal, tenantId)) {
      setTenant(null);
      setDiscordIdentity(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    void Promise.all([
      getTenant(credentials, tenantId),
      getTenantDiscordIdentity(credentials, tenantId),
    ])
      .then(([tenantRecord, identity]) => {
        if (!cancelled) {
          setTenant(tenantRecord);
          setDiscordIdentity(identity);
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setStatusLine(`Failed to load profile settings: ${(error as Error).message}`);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [credentials, principal, ready, tenantId]);

  async function handleSaveProfile() {
    if (!credentials || !isTenantUser) {
      return;
    }
    setSaving(true);
    try {
      await updateAuthenticatedUserProfile(credentials, {
        full_name: fullName.trim(),
      });
      if (membership) {
        await updateTenantUserSettings(credentials, tenantId, {
          mode_override: modeOverride || null,
        });
      }
      await refreshPrincipal();
      setStatusLine("Profile updated.");
    } catch (error) {
      setStatusLine(`Unable to update profile: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function handleChangePassword() {
    if (!credentials) {
      return;
    }
    setPasswordBusy(true);
    try {
      await changeTenantUserPassword(credentials, {
        current_password: currentPassword,
        new_password: nextPassword,
      });
      setCurrentPassword("");
      setNextPassword("");
      setStatusLine("Password updated.");
    } catch (error) {
      setStatusLine(`Unable to update password: ${(error as Error).message}`);
    } finally {
      setPasswordBusy(false);
    }
  }

  async function handleLinkDiscord() {
    if (!credentials) {
      return;
    }
    try {
      const result = await startTenantDiscordLink(credentials, tenantId, `/${encodeURIComponent(tenantId)}/profile`);
      window.location.href = result.authorize_url;
    } catch (error) {
      setStatusLine(`Unable to start Discord link: ${(error as Error).message}`);
    }
  }

  async function handleCreateDiscordInvite() {
    if (!credentials) {
      return;
    }
    try {
      const invite = await createTenantDiscordInvite(credentials, tenantId);
      await navigator.clipboard.writeText(invite.invite_url);
      setStatusLine("Discord invite generated and copied to clipboard.");
    } catch (error) {
      setStatusLine(`Unable to generate Discord invite: ${(error as Error).message}`);
    }
  }

  if (loading) {
    return (
      <div className="rounded-2xl border bg-background p-6">
        <div className="space-y-3">
          <Skeleton className="h-7 w-40" />
          <Skeleton className="h-4 w-60" />
          <div className="grid gap-6 xl:grid-cols-[280px_minmax(0,1fr)]">
            <Skeleton className="h-64 w-full rounded-2xl" />
            <div className="space-y-3">
              <Skeleton className="h-28 w-full rounded-2xl" />
              <Skeleton className="h-28 w-full rounded-2xl" />
              <Skeleton className="h-28 w-full rounded-2xl" />
            </div>
          </div>
        </div>
      </div>
    );
  }

  const tenantDefaultMode = String(tenant?.experience?.default_mode ?? "technical");
  const workspaceName = tenant?.name ?? tenantId;
  const linkedDiscordName =
    discordIdentity?.discord_global_name ?? discordIdentity?.discord_username ?? discordIdentity?.discord_user_id ?? null;
  const discordOauthConfigured = Boolean(discordIdentity?.oauth_configured);
  const workspaceInviteAvailable = Boolean(tenant?.discord?.guild_id);

  function initialsFor(name: string): string {
    const parts = name.trim().split(/\s+/).filter(Boolean);
    if (parts.length === 0) {
      return "MB";
    }
    if (parts.length === 1) {
      return parts[0].slice(0, 2).toUpperCase();
    }
    return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
  }

  function formatModeLabel(value: string | null | undefined): string {
    if (!value) {
      return "Workspace default";
    }
    return value === "non_technical" ? "Business view" : "Technical view";
  }

  return (
    <div className="space-y-6">
      {statusLine ? <div className={`rounded-xl border px-4 py-3 text-sm ${statusClasses}`}>{statusLine}</div> : null}

      <div className="grid gap-6 xl:grid-cols-[280px_minmax(0,1fr)]">
        <aside className="rounded-2xl border bg-background p-5">
          <div className="flex items-center gap-4">
            <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-slate-950 text-lg font-semibold text-white">
              {initialsFor(displayName)}
            </div>
            <div className="min-w-0">
              <p className="truncate text-base font-semibold">{displayName}</p>
              <p className="truncate text-sm text-muted-foreground">{profileIdentifier}</p>
            </div>
          </div>

          <div className="mt-6 space-y-4 border-t pt-4">
            <div>
              <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Workspace</p>
              <p className="mt-1 text-sm font-medium">{workspaceName}</p>
            </div>
            {membership ? (
              <>
                <div>
                  <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Role</p>
                  <p className="mt-1 text-sm">{membership.role.replace(/_/g, " ")}</p>
                </div>
                <div>
                  <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Current view</p>
                  <p className="mt-1 text-sm">{formatModeLabel(membership.effective_mode)}</p>
                </div>
                <div>
                  <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Discord</p>
                  <p className="mt-1 text-sm">{linkedDiscordName ?? "Not linked"}</p>
                </div>
              </>
            ) : (
              <div className="rounded-xl bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
                {isPlatformSuperAdmin
                  ? "Platform super admin access is active for this workspace."
                  : "Workspace-specific details appear here when this user belongs to the workspace."}
              </div>
            )}
          </div>
        </aside>

        <section className="overflow-hidden rounded-2xl border bg-background">
          <div className="divide-y">
            {section === "profile" ? (
              <>
                <section className="grid gap-5 p-6 md:grid-cols-[minmax(0,1fr)_220px] md:items-start">
                  <div className="space-y-4">
                    <div>
                      <div className="flex items-center gap-2">
                        <UserRound className="h-4 w-4 text-muted-foreground" />
                        <h2 className="text-base font-semibold">Account</h2>
                      </div>
                      <p className="mt-1 text-sm text-muted-foreground">These details identify you across Master Builder.</p>
                    </div>
                    <div className="grid gap-4 md:grid-cols-2">
                      <div className="space-y-2">
                        <label className="text-sm font-medium" htmlFor="profile-full-name">
                          {canEditAccountProfile ? "Full name" : "Account"}
                        </label>
                        <Input
                          id="profile-full-name"
                          value={canEditAccountProfile ? fullName : profileIdentifier}
                          onChange={(event) => setFullName(event.target.value)}
                          readOnly={!canEditAccountProfile}
                        />
                      </div>
                      <div className="space-y-2">
                        <label className="text-sm font-medium" htmlFor="profile-email">
                          {principal?.email ? "Email" : "Username"}
                        </label>
                        <Input id="profile-email" value={profileIdentifier} readOnly />
                      </div>
                    </div>
                  </div>
                  {canEditAccountProfile ? (
                    <div className="flex md:justify-end">
                      <Button onClick={() => void handleSaveProfile()} disabled={saving || !fullName.trim()}>
                        {saving ? "Saving..." : "Save profile"}
                      </Button>
                    </div>
                  ) : null}
                </section>

                {membership ? (
                  <section className="grid gap-5 p-6 md:grid-cols-[minmax(0,1fr)_220px] md:items-start">
                    <div className="space-y-4">
                      <div>
                        <div className="flex items-center gap-2">
                          <Check className="h-4 w-4 text-muted-foreground" />
                          <h2 className="text-base font-semibold">Workspace view</h2>
                        </div>
                        <p className="mt-1 text-sm text-muted-foreground">Choose which view opens by default in this workspace.</p>
                      </div>
                      <div className="max-w-sm space-y-2">
                        <label className="text-sm font-medium" htmlFor="profile-mode-override">
                          Experience preference
                        </label>
                        <select
                          id="profile-mode-override"
                          className="w-full rounded-xl border bg-background px-3 py-2 text-sm"
                          value={modeOverride}
                          onChange={(event) => setModeOverride(event.target.value as "technical" | "non_technical" | "")}
                        >
                          <option value="">Workspace default ({formatModeLabel(tenantDefaultMode)})</option>
                          <option value="non_technical">Business view</option>
                          <option value="technical">Technical view</option>
                        </select>
                      </div>
                    </div>
                  </section>
                ) : null}

                {membership ? (
                  <section className="grid gap-5 p-6 md:grid-cols-[minmax(0,1fr)_220px] md:items-start">
                    <div className="space-y-4">
                      <div>
                        <div className="flex items-center gap-2">
                          <DiscordLogo className="h-4 w-4 text-[#5865F2]" />
                          <h2 className="text-base font-semibold">Discord</h2>
                        </div>
                        <p className="mt-1 text-sm text-muted-foreground">Link your Discord profile and open a workspace join link when needed.</p>
                      </div>
                      <div className="grid gap-3 md:grid-cols-2">
                        <div className="rounded-xl border px-4 py-3">
                          <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Identity</p>
                          <p className="mt-2 text-sm font-medium">{linkedDiscordName ?? "Not linked"}</p>
                        </div>
                        <div className="rounded-xl border px-4 py-3">
                          <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">Workspace invite</p>
                          <p className="mt-2 text-sm font-medium">
                            {workspaceInviteAvailable ? "Available" : "Not configured"}
                          </p>
                        </div>
                      </div>
                    </div>
                    <div className="flex flex-col gap-2 md:items-end">
                      {discordOauthConfigured ? (
                        <Button variant={discordIdentity?.linked ? "outline" : "default"} onClick={() => void handleLinkDiscord()}>
                          <DiscordLogo className="mr-2 h-4 w-4" />
                          {discordIdentity?.linked ? "Relink Discord" : "Link Discord"}
                        </Button>
                      ) : (
                        <p className="max-w-[220px] text-right text-sm text-muted-foreground">
                          Discord linking is unavailable until platform Discord OAuth is configured.
                        </p>
                      )}
                      {workspaceInviteAvailable ? (
                        <Button variant="outline" onClick={() => void handleCreateDiscordInvite()}>
                          Copy join link
                        </Button>
                      ) : null}
                    </div>
                  </section>
                ) : null}
              </>
            ) : null}

            {section === "security" ? (
              <section className="grid gap-5 p-6 md:grid-cols-[minmax(0,1fr)_220px] md:items-start">
                <div className="space-y-4">
                  <div>
                    <div className="flex items-center gap-2">
                      <Shield className="h-4 w-4 text-muted-foreground" />
                      <h2 className="text-base font-semibold">Security</h2>
                    </div>
                    <p className="mt-1 text-sm text-muted-foreground">Use a new password with at least eight characters.</p>
                  </div>
                  <div className="grid gap-4 md:grid-cols-2">
                    <div className="space-y-2">
                      <label className="text-sm font-medium" htmlFor="current-password">
                        Current password
                      </label>
                      <Input
                        id="current-password"
                        type="password"
                        value={currentPassword}
                        onChange={(event) => setCurrentPassword(event.target.value)}
                      />
                    </div>
                    <div className="space-y-2">
                      <label className="text-sm font-medium" htmlFor="new-password">
                        New password
                      </label>
                      <Input
                        id="new-password"
                        type="password"
                        value={nextPassword}
                        onChange={(event) => setNextPassword(event.target.value)}
                      />
                    </div>
                  </div>
                </div>
                <div className="flex md:justify-end">
                  <Button
                    onClick={() => void handleChangePassword()}
                    disabled={passwordBusy || !currentPassword || nextPassword.length < 8}
                  >
                    {passwordBusy ? "Updating..." : "Update password"}
                  </Button>
                </div>
              </section>
            ) : null}
          </div>
        </section>
      </div>
    </div>
  );
}
