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
    <div className="space-y-4">
      <div className="rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">Discord install</p>
        <p>Install the shared Master Builder Discord bot into the tenant server, then confirm the onboarding channel here.</p>
      </div>

      <p className="rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
        <CheckCircle2 className="mr-1 inline h-4 w-4" />
        Tenant <strong>{createdTenantId || tenantIdPreview}</strong> is ready for Discord install.
      </p>

      <div className="flex flex-wrap gap-2">
        <Button onClick={onStartInstall}>
          <Link2 className="mr-2 h-4 w-4" />
          {guildId ? "Reinstall Discord Bot" : "Install Discord Bot"}
        </Button>
      </div>

      <div className="rounded-md border p-3 text-sm">
        <div className="flex items-center gap-2 font-medium">
          <Disc3 className="h-4 w-4" />
          Connected guild
        </div>
        <p className="mt-2 text-muted-foreground">
          Guild ID: <strong>{guildId ?? "not connected yet"}</strong>
        </p>
        <p className="text-xs text-muted-foreground">
          Installed at: <strong>{installedAt ?? "not installed yet"}</strong>
        </p>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <div className="space-y-1.5">
          <label className="text-sm font-medium" htmlFor="discord-onboarding-channel">
            Onboarding channel ID
          </label>
          <Input
            id="discord-onboarding-channel"
            value={onboardingChannelId ?? ""}
            onChange={(event) => onOnboardingChannelChange(event.target.value)}
            placeholder="Discord text channel ID"
          />
        </div>
        <div className="space-y-1.5">
          <label className="text-sm font-medium" htmlFor="discord-invite-expiry">
            Invite expiry seconds
          </label>
          <Input
            id="discord-invite-expiry"
            value={String(inviteExpirySeconds ?? 86400)}
            onChange={(event) => onInviteExpiryChange(event.target.value)}
            placeholder="86400"
          />
        </div>
        <div className="space-y-1.5">
          <label className="text-sm font-medium" htmlFor="discord-invite-max-uses">
            Invite max uses
          </label>
          <Input
            id="discord-invite-max-uses"
            value={String(inviteMaxUses ?? 1)}
            onChange={(event) => onInviteMaxUsesChange(event.target.value)}
            placeholder="1"
          />
        </div>
      </div>
    </div>
  );
}
