import { CheckCircle2, Disc3, Link2 } from "lucide-react";

import { Button } from "@/components/ui/button";
type DiscordStepProps = {
  createdTenantId: string;
  tenantIdPreview: string;
  guildId: string | null | undefined;
  installedAt: string | null | undefined;
  onStartInstall: () => void;
};

export function DiscordStep({
  createdTenantId,
  tenantIdPreview,
  guildId,
  installedAt,
  onStartInstall
}: DiscordStepProps) {
  return (
    <div className="max-w-xl">
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
            <div className="text-xs text-slate-500">Optional step. You can continue setup without Discord.</div>
          </div>
        </div>
      </div>
    </div>
  );
}
