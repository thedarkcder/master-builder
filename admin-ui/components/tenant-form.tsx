"use client";

import { useMemo, useState, type ReactNode } from "react";

import type { TenantCreatePayload, TenantUpdatePayload } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  defaultTenantFormValues,
  formValuesToTextFields,
  type TenantFormValues,
  toCreatePayload,
  toUpdatePayload
} from "@/lib/tenant-form";

type TenantFormProps = {
  submitting?: boolean;
} & (
  | {
      mode: "create";
      initialValues?: TenantFormValues;
      onSubmit: (payload: TenantCreatePayload) => Promise<void>;
    }
  | {
      mode: "edit";
      initialValues: TenantFormValues;
      onSubmit: (payload: TenantUpdatePayload) => Promise<void>;
    }
);

function FieldLabel({ children }: { children: ReactNode }) {
  return <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{children}</label>;
}

function Toggle({
  label,
  checked,
  onChange
}: {
  label: string;
  checked: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <input
        type="checkbox"
        className="h-4 w-4 rounded border-input"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      {label}
    </label>
  );
}

export function TenantForm({ mode, initialValues, onSubmit, submitting = false }: TenantFormProps) {
  const [values, setValues] = useState<TenantFormValues>(initialValues ?? defaultTenantFormValues());
  const [error, setError] = useState("");
  const [textFields, setTextFields] = useState(() => formValuesToTextFields(initialValues ?? defaultTenantFormValues()));

  const submitLabel = mode === "create" ? "Create Tenant" : "Save Tenant";

  const validation = useMemo(() => {
    if (!values.name.trim()) {
      return "Tenant name is required.";
    }
    if (!values.jira.mcp_endpoint.trim()) {
      return "Jira endpoint is required.";
    }
    if (!textFields.projectKeysText.trim()) {
      return "At least one Jira project key is required.";
    }
    if (!textFields.allowlistText.trim()) {
      return "At least one repository allowlist entry is required.";
    }
    return "";
  }, [mode, textFields.allowlistText, textFields.projectKeysText, values]);

  async function handleSubmit(): Promise<void> {
    if (validation) {
      setError(validation);
      return;
    }

    setError("");
    if (mode === "create") {
      await onSubmit(toCreatePayload(values, textFields));
      return;
    }

    await onSubmit(toUpdatePayload(values, textFields));
  }

  return (
    <div className="space-y-4">
      {error ? <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p> : null}

      <Card>
        <CardHeader>
          <CardTitle>Identity</CardTitle>
          <CardDescription>Tenant identity and activation state. Tenant ID is generated from name.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-2">
          <div className="space-y-2">
            <FieldLabel>Tenant ID</FieldLabel>
            <Input
              disabled
              value={values.tenantId}
              placeholder="generated from tenant name"
            />
          </div>
          <div className="space-y-2">
            <FieldLabel>Name</FieldLabel>
            <Input
              value={values.name}
              onChange={(event) => setValues((prev) => ({ ...prev, name: event.target.value }))}
              placeholder="Tenant Demo"
            />
          </div>
          <Toggle
            label="Tenant enabled"
            checked={values.isEnabled}
            onChange={(next) => setValues((prev) => ({ ...prev, isEnabled: next }))}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Jira</CardTitle>
          <CardDescription>Jira webhook and issue discovery configuration.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-2">
          <div className="space-y-2 md:col-span-2">
            <FieldLabel>MCP endpoint</FieldLabel>
            <Input
              value={values.jira.mcp_endpoint}
              onChange={(event) =>
                setValues((prev) => ({ ...prev, jira: { ...prev.jira, mcp_endpoint: event.target.value } }))
              }
              placeholder="https://mcp.example.test"
            />
          </div>
          <div className="space-y-2">
            <FieldLabel>Project keys (comma separated)</FieldLabel>
            <Input
              value={textFields.projectKeysText}
              onChange={(event) => setTextFields((prev) => ({ ...prev, projectKeysText: event.target.value }))}
              placeholder="TP, APP"
            />
          </div>
          <div className="space-y-2">
            <FieldLabel>Ready label</FieldLabel>
            <Input
              value={values.jira.ready_label}
              onChange={(event) =>
                setValues((prev) => ({ ...prev, jira: { ...prev.jira, ready_label: event.target.value } }))
              }
              placeholder="agent:ready"
            />
          </div>
          <div className="space-y-2">
            <FieldLabel>In progress label</FieldLabel>
            <Input
              value={values.jira.in_progress_label}
              onChange={(event) =>
                setValues((prev) => ({ ...prev, jira: { ...prev.jira, in_progress_label: event.target.value } }))
              }
              placeholder="agent:in-progress"
            />
          </div>
          <div className="space-y-2">
            <FieldLabel>Blocked label</FieldLabel>
            <Input
              value={values.jira.blocked_label}
              onChange={(event) =>
                setValues((prev) => ({ ...prev, jira: { ...prev.jira, blocked_label: event.target.value } }))
              }
              placeholder="agent:blocked"
            />
          </div>
          <div className="space-y-2">
            <FieldLabel>Done label (optional)</FieldLabel>
            <Input
              value={values.jira.done_label ?? ""}
              onChange={(event) =>
                setValues((prev) => ({ ...prev, jira: { ...prev.jira, done_label: event.target.value || null } }))
              }
              placeholder="agent:done"
            />
          </div>
          <div className="space-y-2 md:col-span-2">
            <FieldLabel>Webhook secret ref (optional)</FieldLabel>
            <Input
              value={values.jira.webhook_secret_ref ?? ""}
              onChange={(event) =>
                setValues((prev) => ({ ...prev, jira: { ...prev.jira, webhook_secret_ref: event.target.value || null } }))
              }
              placeholder="secret/jira-webhook"
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>GitHub</CardTitle>
          <CardDescription>GitHub App connection and webhook settings.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-2">
          <div className="space-y-2">
            <FieldLabel>Mode</FieldLabel>
            <Input
              value={values.github.mode}
              onChange={(event) => setValues((prev) => ({ ...prev, github: { ...prev.github, mode: event.target.value } }))}
              placeholder="github_app"
            />
          </div>
          <div className="rounded-md border border-dashed bg-muted/40 px-3 py-2 text-xs text-muted-foreground md:col-span-2">
            Installation ID is set by the Connect GitHub App flow. App credentials are managed server-side.
          </div>
          <div className="space-y-2 md:col-span-2">
            <FieldLabel>Webhook secret ref (optional)</FieldLabel>
            <Input
              value={values.github.webhook_secret_ref ?? ""}
              onChange={(event) =>
                setValues((prev) => ({ ...prev, github: { ...prev.github, webhook_secret_ref: event.target.value || null } }))
              }
              placeholder="secret/github-webhook"
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Repository Mapping</CardTitle>
          <CardDescription>Define the target repos and mapping rules.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-2">
            <FieldLabel>Allowlist (one URL per line)</FieldLabel>
            <Textarea
              value={textFields.allowlistText}
              onChange={(event) => setTextFields((prev) => ({ ...prev, allowlistText: event.target.value }))}
              className="min-h-[110px]"
              placeholder="https://github.com/example/repo"
            />
          </div>
          <div className="space-y-2">
            <FieldLabel>Rules by Jira project key (one per line: key=url)</FieldLabel>
            <Textarea
              value={textFields.mappingByProjectText}
              onChange={(event) => setTextFields((prev) => ({ ...prev, mappingByProjectText: event.target.value }))}
              className="min-h-[110px]"
              placeholder="TP=https://github.com/example/repo"
            />
          </div>
          <div className="space-y-2">
            <FieldLabel>Rules by Jira component (one per line: component=url)</FieldLabel>
            <Textarea
              value={textFields.mappingByComponentText}
              onChange={(event) => setTextFields((prev) => ({ ...prev, mappingByComponentText: event.target.value }))}
              className="min-h-[110px]"
              placeholder="payments=https://github.com/example/payments"
            />
          </div>
          <div className="space-y-2">
            <FieldLabel>Fallback repo (optional)</FieldLabel>
            <Input
              value={values.repos.fallback_repo ?? ""}
              onChange={(event) =>
                setValues((prev) => ({ ...prev, repos: { ...prev.repos, fallback_repo: event.target.value || null } }))
              }
              placeholder="https://github.com/example/default"
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Policy</CardTitle>
          <CardDescription>Execution limits and safety policy.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 md:grid-cols-3">
            <div className="space-y-2">
              <FieldLabel>Max runtime (minutes)</FieldLabel>
              <Input
                type="number"
                value={String(values.policy.max_runtime_minutes)}
                onChange={(event) =>
                  setValues((prev) => ({
                    ...prev,
                    policy: { ...prev.policy, max_runtime_minutes: Number(event.target.value || 0) }
                  }))
                }
              />
            </div>
            <div className="space-y-2">
              <FieldLabel>Max loops</FieldLabel>
              <Input
                type="number"
                value={String(values.policy.max_dev_test_review_loops)}
                onChange={(event) =>
                  setValues((prev) => ({
                    ...prev,
                    policy: { ...prev.policy, max_dev_test_review_loops: Number(event.target.value || 0) }
                  }))
                }
              />
            </div>
            <div className="space-y-2">
              <FieldLabel>Max concurrent runs</FieldLabel>
              <Input
                type="number"
                value={String(values.policy.max_concurrent_runs)}
                onChange={(event) =>
                  setValues((prev) => ({
                    ...prev,
                    policy: { ...prev.policy, max_concurrent_runs: Number(event.target.value || 0) }
                  }))
                }
              />
            </div>
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            <Toggle
              label="Allow Jira transitions"
              checked={values.policy.allow_jira_transitions}
              onChange={(next) =>
                setValues((prev) => ({ ...prev, policy: { ...prev.policy, allow_jira_transitions: next } }))
              }
            />
            <Toggle
              label="Allow PR creation"
              checked={values.policy.allow_pr_creation}
              onChange={(next) => setValues((prev) => ({ ...prev, policy: { ...prev.policy, allow_pr_creation: next } }))}
            />
            <Toggle
              label="Allow label mutations"
              checked={values.policy.allow_label_mutations}
              onChange={(next) =>
                setValues((prev) => ({ ...prev, policy: { ...prev.policy, allow_label_mutations: next } }))
              }
            />
            <Toggle
              label="Require AGENTS.md"
              checked={values.policy.require_agents_md}
              onChange={(next) => setValues((prev) => ({ ...prev, policy: { ...prev.policy, require_agents_md: next } }))}
            />
          </div>
          <div className="space-y-2">
            <FieldLabel>Allowed commands (one per line)</FieldLabel>
            <Textarea
              value={textFields.policyAllowedCommandsText}
              onChange={(event) =>
                setTextFields((prev) => ({ ...prev, policyAllowedCommandsText: event.target.value }))
              }
              placeholder="git status"
              className="min-h-[110px]"
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Discord (optional)</CardTitle>
          <CardDescription>Notification channel defaults for this tenant.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Toggle
            label="Enable Discord settings"
            checked={values.discordEnabled}
            onChange={(next) => setValues((prev) => ({ ...prev, discordEnabled: next }))}
          />

          {values.discordEnabled ? (
            <>
              <div className="space-y-2">
                <FieldLabel>Channel ID (optional)</FieldLabel>
                <Input
                  value={values.discord.channel_id ?? ""}
                  onChange={(event) =>
                    setValues((prev) => ({
                      ...prev,
                      discord: { ...prev.discord, channel_id: event.target.value || null }
                    }))
                  }
                  placeholder="123456789012345"
                />
              </div>
              <div className="space-y-2">
                <FieldLabel>Channel name template</FieldLabel>
                <Input
                  value={values.discord.channel_name_template}
                  onChange={(event) =>
                    setValues((prev) => ({
                      ...prev,
                      discord: { ...prev.discord, channel_name_template: event.target.value }
                    }))
                  }
                  placeholder="proj-{tenant_id}"
                />
              </div>
              <div className="space-y-2">
                <FieldLabel>Notify events (one per line)</FieldLabel>
                <Textarea
                  value={textFields.notifyEventsText}
                  onChange={(event) => setTextFields((prev) => ({ ...prev, notifyEventsText: event.target.value }))}
                  placeholder="run.started"
                />
              </div>
            </>
          ) : null}
        </CardContent>
      </Card>

      <div className="flex justify-end">
        <Button onClick={handleSubmit} disabled={submitting}>
          {submitting ? "Saving..." : submitLabel}
        </Button>
      </div>
    </div>
  );
}
