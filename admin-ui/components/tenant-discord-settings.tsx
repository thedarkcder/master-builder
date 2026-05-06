"use client";

import { useEffect, useState } from "react";
import { Link2 } from "lucide-react";

import type { TenantDiscordUpdatePayload, TenantRecord } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type TenantDiscordSettingsProps = {
  tenant: TenantRecord;
  saving: boolean;
  onSave: (payload: TenantDiscordUpdatePayload) => Promise<void>;
  onInstall: () => void;
};

type TenantDiscordFormState = {
  enabled: boolean;
  serverId: string;
  onboardingChannelId: string;
  inviteExpirySeconds: string;
  inviteMaxUses: string;
};

function formStateFromTenant(tenant: TenantRecord): TenantDiscordFormState {
  return {
    enabled: Boolean(tenant.discord),
    serverId: tenant.discord?.guild_id ?? "",
    onboardingChannelId: tenant.discord?.onboarding_channel_id ?? "",
    inviteExpirySeconds: String(tenant.discord?.onboarding_invite_expires_in_seconds ?? 86400),
    inviteMaxUses: String(tenant.discord?.onboarding_invite_max_uses ?? 1)
  };
}

function buildDiscordPayload(tenant: TenantRecord, form: TenantDiscordFormState): TenantDiscordUpdatePayload {
  return {
    discord: form.enabled
      ? {
          ...(tenant.discord ?? {}),
          guild_id: form.serverId.trim() || null,
          onboarding_channel_id: form.onboardingChannelId.trim() || null,
          onboarding_invite_expires_in_seconds: Number(form.inviteExpirySeconds || "0") || null,
          onboarding_invite_max_uses: Number(form.inviteMaxUses || "0") || null,
          notify_events: tenant.discord?.notify_events ?? []
        }
      : null
  };
}

export function TenantDiscordSettings({ tenant, saving, onSave, onInstall }: TenantDiscordSettingsProps) {
  const [form, setForm] = useState<TenantDiscordFormState>(() => formStateFromTenant(tenant));

  useEffect(() => {
    setForm(formStateFromTenant(tenant));
  }, [tenant]);

  async function handleSave(): Promise<void> {
    await onSave(buildDiscordPayload(tenant, form));
  }

  return (
    <div className="overflow-hidden rounded-2xl border bg-background">
      <div className="px-6 pt-6">
        <h2 className="text-base font-semibold">Discord Integration</h2>
      </div>
      <div className="space-y-4 p-6">
        <div className="space-y-2 text-sm">
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              className="h-4 w-4 rounded border-input"
              checked={form.enabled}
              onChange={(event) => setForm((prev) => ({ ...prev, enabled: event.target.checked }))}
            />
            <span>Enable Discord</span>
          </label>
          <div className="rounded-xl border bg-background px-4 py-3 text-xs text-muted-foreground">
            Connected guild: <strong>{tenant.discord?.guild_id ?? "not installed yet"}</strong>
            <br />
            Installed at: <strong>{tenant.discord?.installed_at ?? "not installed yet"}</strong>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={onInstall}>
              <Link2 className="mr-2 h-4 w-4" />
              {tenant.discord?.guild_id ? "Reinstall Discord Bot" : "Install Discord Bot"}
            </Button>
          </div>
          <div className="space-y-1">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Server ID</p>
            <Input
              value={form.serverId}
              onChange={(event) => setForm((prev) => ({ ...prev, serverId: event.target.value }))}
              placeholder="Discord guild/server ID"
              disabled={!form.enabled}
            />
          </div>
          <div className="space-y-1">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Onboarding Channel ID</p>
            <Input
              value={form.onboardingChannelId}
              onChange={(event) => setForm((prev) => ({ ...prev, onboardingChannelId: event.target.value }))}
              placeholder="Discord channel used for join invites"
              disabled={!form.enabled}
            />
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1">
              <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Invite expiry seconds</p>
              <Input
                value={form.inviteExpirySeconds}
                onChange={(event) => setForm((prev) => ({ ...prev, inviteExpirySeconds: event.target.value }))}
                placeholder="86400"
                disabled={!form.enabled}
              />
            </div>
            <div className="space-y-1">
              <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Invite max uses</p>
              <Input
                value={form.inviteMaxUses}
                onChange={(event) => setForm((prev) => ({ ...prev, inviteMaxUses: event.target.value }))}
                placeholder="1"
                disabled={!form.enabled}
              />
            </div>
          </div>
          <div className="rounded-xl border bg-background px-4 py-3 text-xs text-muted-foreground">
            Live voice rooms are configured per project on the project Discord page. Onboarding joins use the tenant onboarding channel.
          </div>
          <Button onClick={() => void handleSave()} disabled={saving}>
            {saving ? "Saving..." : "Save Discord settings"}
          </Button>
        </div>
      </div>
    </div>
  );
}
