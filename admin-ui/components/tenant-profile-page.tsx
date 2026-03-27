"use client";

import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
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

export function TenantProfilePage() {
  const params = useParams<{ tenantId: string }>();
  const { credentials, principal, ready, refreshPrincipal } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);
  const isTenantUser = principal?.principal_type === "tenant_user";
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

  const statusClasses = useMemo(() => {
    const normalized = statusLine.toLowerCase();
    if (normalized.includes("failed") || normalized.includes("unable") || normalized.includes("incorrect")) {
      return "border-red-300 bg-red-50 text-red-800";
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
    if (!ready || !credentials || !membership) {
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
  }, [credentials, membership, ready, tenantId]);

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
      const result = await startTenantDiscordLink(credentials, tenantId, `/tenants/${encodeURIComponent(tenantId)}/profile`);
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
      <Card>
        <CardHeader>
          <Skeleton className="h-6 w-48" />
          <Skeleton className="h-4 w-72" />
        </CardHeader>
        <CardContent className="space-y-3">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </CardContent>
      </Card>
    );
  }

  const tenantDefaultMode = String(tenant?.experience?.default_mode ?? "technical");

  return (
    <div className="space-y-4">
      <div className="mb-1">
        <h1 className="text-xl font-semibold">Profile</h1>
        <p className="text-sm text-muted-foreground">Manage your details, experience mode, Discord identity, and password.</p>
      </div>

      {statusLine ? <div className={`rounded-md border px-3 py-2 text-sm ${statusClasses}`}>{statusLine}</div> : null}

      <Card>
        <CardHeader>
          <CardTitle>Profile details</CardTitle>
          <CardDescription>Your account details in Master Builder.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-2">
          <div className="space-y-2">
            <label className="text-sm font-medium" htmlFor="profile-full-name">
              Full name
            </label>
            <Input id="profile-full-name" value={fullName} onChange={(event) => setFullName(event.target.value)} />
          </div>
          <div className="space-y-2">
            <label className="text-sm font-medium" htmlFor="profile-email">
              {principal?.email ? "Email" : "Username"}
            </label>
            <Input id="profile-email" value={profileIdentifier} readOnly />
          </div>
          {membership ? (
            <>
              <div className="space-y-2">
                <label className="text-sm font-medium" htmlFor="profile-role">
                  Role
                </label>
                <Input id="profile-role" value={membership.role.replace(/_/g, " ")} readOnly />
              </div>
              <div className="space-y-2">
                <label className="text-sm font-medium" htmlFor="profile-effective-mode">
                  Effective experience
                </label>
                <Input id="profile-effective-mode" value={membership.effective_mode.replace(/_/g, " ")} readOnly />
              </div>
              <div className="space-y-2 md:col-span-2">
                <label className="text-sm font-medium" htmlFor="profile-mode-override">
                  Experience preference
                </label>
                <select
                  id="profile-mode-override"
                  className="w-full rounded-md border bg-background px-3 py-2"
                  value={modeOverride}
                  onChange={(event) => setModeOverride(event.target.value as "technical" | "non_technical" | "")}
                >
                  <option value="">Use tenant default ({tenantDefaultMode.replace(/_/g, " ")})</option>
                  <option value="non_technical">Non-technical</option>
                  <option value="technical">Technical</option>
                </select>
              </div>
            </>
          ) : (
            <div className="rounded-md border bg-muted/30 px-3 py-2 text-sm text-muted-foreground md:col-span-2">
              Workspace-specific experience settings are only available when this account belongs to the current workspace.
            </div>
          )}
          <div className="md:col-span-2">
            <Button onClick={() => void handleSaveProfile()} disabled={saving || !isTenantUser || !fullName.trim()}>
              {saving ? "Saving..." : "Save profile"}
            </Button>
          </div>
        </CardContent>
      </Card>

      {membership ? (
        <Card>
          <CardHeader>
            <CardTitle>Discord</CardTitle>
            <CardDescription>Link your Discord identity and generate a tenant join invite for yourself.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <p>
              <strong>Identity:</strong>{" "}
              {discordIdentity?.linked
                ? `${discordIdentity.discord_global_name ?? discordIdentity.discord_username ?? discordIdentity.discord_user_id}`
                : "Not linked"}
            </p>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" onClick={() => void handleLinkDiscord()}>
                Link Discord
              </Button>
              <Button variant="outline" onClick={() => void handleCreateDiscordInvite()}>
                Generate Join Invite
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {isTenantUser ? (
        <Card>
          <CardHeader>
            <CardTitle>Password</CardTitle>
            <CardDescription>Change your password for Master Builder.</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4 md:grid-cols-2">
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
            <div className="md:col-span-2">
              <Button
                onClick={() => void handleChangePassword()}
                disabled={passwordBusy || !currentPassword || nextPassword.length < 8}
              >
                {passwordBusy ? "Updating..." : "Update password"}
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>Password</CardTitle>
            <CardDescription>Platform administrator passwords are managed through platform administration, not tenant profile settings.</CardDescription>
          </CardHeader>
        </Card>
      )}
    </div>
  );
}
