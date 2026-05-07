"use client";

import { useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/components/ui/toast-provider";
import { updateTenantObservability, type TenantRecord } from "@/lib/api";

const AUDIT_RETENTION_OPTIONS = [
  { value: 30, label: "30 days" },
  { value: 90, label: "90 days" },
  { value: 180, label: "180 days" },
  { value: 365, label: "1 year" },
  { value: 730, label: "2 years" },
  { value: 2555, label: "7 years" }
] as const;

type TenantObservabilitySettingsProps = {
  tenant: TenantRecord;
  onTenantUpdated: (tenant: TenantRecord) => void;
  onStatus: (message: string) => void;
};

type TenantObservabilityFormState = {
  auditRetentionDays: string;
  auditExportEnabled: boolean;
  legalHoldEnabled: boolean;
  legalHoldReason: string;
};

function formStateFromTenant(tenant: TenantRecord): TenantObservabilityFormState {
  return {
    auditRetentionDays: String(tenant.policy.observability?.audit_retention_days ?? 365),
    auditExportEnabled: tenant.policy.observability?.audit_export_enabled ?? true,
    legalHoldEnabled: tenant.policy.observability?.legal_hold_enabled ?? false,
    legalHoldReason: tenant.policy.observability?.legal_hold_reason ?? ""
  };
}

export function TenantObservabilitySettings({
  tenant,
  onTenantUpdated,
  onStatus
}: TenantObservabilitySettingsProps) {
  const { credentials } = useAuth();
  const { showToast } = useToast();
  const [form, setForm] = useState<TenantObservabilityFormState>(() => formStateFromTenant(tenant));
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setForm(formStateFromTenant(tenant));
  }, [tenant]);

  async function saveObservabilitySettings(): Promise<void> {
    if (!credentials) {
      return;
    }
    if (form.legalHoldEnabled && !form.legalHoldReason.trim()) {
      onStatus("Legal hold reason is required when legal hold is enabled.");
      return;
    }
    setSaving(true);
    try {
      const updated = await updateTenantObservability(credentials, tenant.tenant_id, {
        observability: {
          audit_retention_days: Number(form.auditRetentionDays || "365") || 365,
          audit_export_enabled: form.auditExportEnabled,
          legal_hold_enabled: form.legalHoldEnabled,
          legal_hold_reason: form.legalHoldEnabled ? form.legalHoldReason.trim() : null
        }
      });
      onTenantUpdated(updated);
      showToast({ title: "Observability policy saved", tone: "success" });
    } catch (error) {
      showToast({ title: "Observability policy save failed", description: (error as Error).message, tone: "error" });
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="overflow-hidden rounded-2xl border bg-background">
      <div className="px-6 pt-6">
        <h2 className="text-base font-semibold">Observability policy</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Configure tenant-level audit retention, export access, and legal hold. These settings control the durable audit plane, not live telemetry backend internals.
        </p>
      </div>
      <div className="space-y-5 p-6">
        <div className="grid gap-5 md:grid-cols-2">
          <div className="space-y-2">
            <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Audit retention</label>
            <select
              aria-label="Audit retention"
              className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
              value={form.auditRetentionDays}
              onChange={(event) => setForm((prev) => ({ ...prev, auditRetentionDays: event.target.value }))}
              disabled={saving}
            >
              {AUDIT_RETENTION_OPTIONS.map((option) => (
                <option key={option.value} value={String(option.value)}>
                  {option.label}
                </option>
              ))}
            </select>
            <p className="text-xs text-muted-foreground">
              Audit history older than this is pruned automatically unless legal hold is active.
            </p>
          </div>
          <div className="space-y-3 rounded-xl border p-4 text-sm">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                className="h-4 w-4 rounded border-input"
                checked={form.auditExportEnabled}
                onChange={(event) => setForm((prev) => ({ ...prev, auditExportEnabled: event.target.checked }))}
                disabled={saving}
              />
              Allow audit export
            </label>
            <p className="text-xs text-muted-foreground">
              Controls whether tenant-scoped audit history can be exported from the admin API.
            </p>
          </div>
        </div>

        <div className="space-y-3 rounded-xl border p-4">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              className="h-4 w-4 rounded border-input"
              checked={form.legalHoldEnabled}
              onChange={(event) => setForm((prev) => ({ ...prev, legalHoldEnabled: event.target.checked }))}
              disabled={saving}
            />
            Enable legal hold
          </label>
          <p className="text-xs text-muted-foreground">
            Prevents audit retention pruning for this tenant until the hold is cleared.
          </p>
          <div className="space-y-2">
            <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Legal hold reason</label>
            <Textarea
              value={form.legalHoldReason}
              onChange={(event) => setForm((prev) => ({ ...prev, legalHoldReason: event.target.value }))}
              placeholder="Example: Customer litigation hold requested on 2026-04-20."
              className="min-h-[110px]"
              disabled={saving || !form.legalHoldEnabled}
            />
          </div>
        </div>

        <div className="flex justify-end">
          <Button onClick={() => void saveObservabilitySettings()} disabled={saving}>
            {saving ? "Saving..." : "Save observability policy"}
          </Button>
        </div>
      </div>
    </div>
  );
}
