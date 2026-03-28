import { CheckCircle2, Disc3, Link2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type DiscordStepProps = {
  createdTenantId: string;
  tenantIdPreview: string;
  guildId: string | null | undefined;
  installedAt: string | null | undefined;
  onboardingChannelId: string | null | undefined;
  inviteExpirySeconds: number | null | undefined;
  inviteMaxUses: number | null | undefined;
  onStartInstall: () => void;
  onOnboardingChannelChange: (value: string) => void;
  onInviteExpiryChange: (value: string) => void;
  onInviteMaxUsesChange: (value: string) => void;
};

export function DiscordStep({
  createdTenantId,
  tenantIdPreview,
  guildId,
  installedAt,
  onboardingChannelId,
  inviteExpirySeconds,
  inviteMaxUses,
  onStartInstall,
  onOnboardingChannelChange,
  onInviteExpiryChange,
  onInviteMaxUsesChange
}: DiscordStepProps) {
  return (
    <div className="grid gap-6 xl:grid-cols-[320px_minmax(0,1fr)]">
      <div className="min-w-0 rounded-2xl border border-slate-200 bg-slate-50/70 px-5 py-5">
        <div className="space-y-4">
          <div className="flex items-center gap-2 text-sm font-medium text-slate-900">
            <CheckCircle2 className="h-4 w-4 text-emerald-600" />
            Workspace ready for Discord install
          </div>
          <div className="text-sm text-slate-600">{createdTenantId || tenantIdPreview}</div>

          <div className="pt-1">
            <Button onClick={onStartInstall}>
              <Link2 className="mr-2 h-4 w-4" />
              {guildId ? "Reinstall Discord Bot" : "Install Discord Bot"}
            </Button>
          </div>

          <div className="space-y-2 border-t border-slate-200 pt-4 text-sm">
            <div className="flex items-center gap-2 font-medium text-slate-900">
              <Disc3 className="h-4 w-4" />
              Connected guild
            </div>
            <div className="text-slate-600">
              Guild ID: <strong>{guildId ?? "Not connected yet"}</strong>
            </div>
            <div className="text-xs text-slate-500">Installed at: <strong>{installedAt ?? "Not installed yet"}</strong></div>
          </div>
        </div>
      </div>

      <div className="min-w-0 grid gap-5 md:grid-cols-2">
        <div className="space-y-1.5 md:col-span-2">
          <label className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500" htmlFor="discord-onboarding-channel">
            Invite channel
          </label>
          <Input
            id="discord-onboarding-channel"
            value={onboardingChannelId ?? ""}
            onChange={(event) => onOnboardingChannelChange(event.target.value)}
            placeholder="Discord text channel ID"
            className="h-12 rounded-2xl border-slate-200 bg-white"
          />
          <p className="text-sm text-slate-500">Use the channel where new members should receive the workspace invite.</p>
        </div>
        <div className="space-y-1.5">
          <label className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500" htmlFor="discord-invite-expiry">
            Invite expiry
          </label>
          <Input
            id="discord-invite-expiry"
            value={String(inviteExpirySeconds ?? 86400)}
            onChange={(event) => onInviteExpiryChange(event.target.value)}
            placeholder="86400"
            className="h-12 rounded-2xl border-slate-200 bg-white"
          />
        </div>
        <div className="space-y-1.5">
          <label className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500" htmlFor="discord-invite-max-uses">
            Invite max uses
          </label>
          <Input
            id="discord-invite-max-uses"
            value={String(inviteMaxUses ?? 1)}
            onChange={(event) => onInviteMaxUsesChange(event.target.value)}
            placeholder="1"
            className="h-12 rounded-2xl border-slate-200 bg-white"
          />
        </div>
      </div>
    </div>
  );
}
