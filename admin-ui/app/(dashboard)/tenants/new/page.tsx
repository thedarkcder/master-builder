"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { ArrowLeft, ArrowRight, CheckCircle2, Link2 } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { createTenant, startGitHubInstall } from "@/lib/api";
import {
  defaultTenantFormValues,
  formValuesToTextFields,
  toCreatePayload,
  type TenantFormTextFields,
  type TenantFormValues
} from "@/lib/tenant-form";

const STEPS = [
  "Tenant Basics",
  "Jira",
  "GitHub",
  "Repositories + Policy",
  "Review + Create",
  "Connect GitHub App"
] as const;

function WizardProgress({ step }: { step: number }) {
  return (
    <ol className="grid gap-2 md:grid-cols-3">
      {STEPS.map((label, index) => {
        const active = index === step;
        const complete = index < step;
        return (
          <li
            key={label}
            className={`rounded-md border px-3 py-2 text-xs ${
              active ? "border-primary bg-primary/10 text-primary" : complete ? "border-emerald-300 bg-emerald-50" : ""
            }`}
          >
            <span className="font-semibold">{index + 1}.</span> {label}
          </li>
        );
      })}
    </ol>
  );
}

export default function NewTenantPage() {
  const { credentials } = useAuth();

  const [values, setValues] = useState<TenantFormValues>(defaultTenantFormValues());
  const [textFields, setTextFields] = useState<TenantFormTextFields>(formValuesToTextFields(defaultTenantFormValues()));
  const [step, setStep] = useState(0);
  const [statusLine, setStatusLine] = useState("Start with tenant basics.");
  const [saving, setSaving] = useState(false);
  const [createdTenantId, setCreatedTenantId] = useState("");

  const canAdvance = useMemo(() => {
    if (step === 0) {
      return Boolean(values.tenantId.trim() && values.name.trim());
    }
    if (step === 1) {
      return Boolean(
        values.jira.mcp_endpoint.trim() &&
          values.jira.auth_ref.trim() &&
          textFields.projectKeysText.trim() &&
          values.jira.ready_jql.trim()
      );
    }
    if (step === 2) {
      return Boolean(values.github.app_id_ref.trim() && values.github.private_key_ref.trim());
    }
    if (step === 3) {
      return Boolean(textFields.allowlistText.trim());
    }
    return true;
  }, [step, textFields.allowlistText, textFields.projectKeysText, values]);

  function nextStep() {
    if (!canAdvance) {
      setStatusLine("Please complete required fields before continuing.");
      return;
    }
    setStep((current) => Math.min(current + 1, STEPS.length - 1));
  }

  function previousStep() {
    setStep((current) => Math.max(current - 1, 0));
  }

  async function handleCreateTenant() {
    if (!credentials) {
      return;
    }

    setSaving(true);
    try {
      const payload = toCreatePayload(values, textFields);
      const created = await createTenant(credentials, payload);
      setCreatedTenantId(created.tenant_id);
      setStatusLine(`Created tenant ${created.tenant_id}. Connect GitHub App next.`);
      setStep(5);
    } catch (error) {
      setStatusLine(`Create failed: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function handleStartInstall() {
    if (!credentials || !createdTenantId) {
      return;
    }
    try {
      const result = await startGitHubInstall(credentials, createdTenantId);
      window.location.href = result.install_url;
    } catch (error) {
      setStatusLine(`Unable to start GitHub App install: ${(error as Error).message}`);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Tenant Setup Wizard</CardTitle>
        <CardDescription>Guided onboarding for tenant creation and GitHub App connection.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <WizardProgress step={step} />

        {step === 0 ? (
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Tenant ID</label>
              <Input
                value={values.tenantId}
                onChange={(event) => setValues((prev) => ({ ...prev, tenantId: event.target.value }))}
                placeholder="tenant-demo"
              />
            </div>
            <div className="space-y-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Tenant Name</label>
              <Input
                value={values.name}
                onChange={(event) => setValues((prev) => ({ ...prev, name: event.target.value }))}
                placeholder="Tenant Demo"
              />
            </div>
          </div>
        ) : null}

        {step === 1 ? (
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-2 md:col-span-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">MCP Endpoint</label>
              <Input
                value={values.jira.mcp_endpoint}
                onChange={(event) =>
                  setValues((prev) => ({ ...prev, jira: { ...prev.jira, mcp_endpoint: event.target.value } }))
                }
                placeholder="https://mcp.example.test"
              />
            </div>
            <div className="space-y-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Auth Ref</label>
              <Input
                value={values.jira.auth_ref}
                onChange={(event) => setValues((prev) => ({ ...prev, jira: { ...prev.jira, auth_ref: event.target.value } }))}
                placeholder="secret/jira"
              />
            </div>
            <div className="space-y-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Project Keys</label>
              <Input
                value={textFields.projectKeysText}
                onChange={(event) => setTextFields((prev) => ({ ...prev, projectKeysText: event.target.value }))}
                placeholder="TP, APP"
              />
            </div>
            <div className="space-y-2 md:col-span-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Ready JQL</label>
              <Input
                value={values.jira.ready_jql}
                onChange={(event) => setValues((prev) => ({ ...prev, jira: { ...prev.jira, ready_jql: event.target.value } }))}
                placeholder="project = TP"
              />
            </div>
          </div>
        ) : null}

        {step === 2 ? (
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">GitHub Mode</label>
              <Input
                value={values.github.mode}
                onChange={(event) => setValues((prev) => ({ ...prev, github: { ...prev.github, mode: event.target.value } }))}
                placeholder="github_app"
              />
            </div>
            <div className="rounded-md border border-dashed bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
              Installation ID is captured automatically in step 6 after install.
            </div>
            <div className="space-y-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">App ID Ref</label>
              <Input
                value={values.github.app_id_ref}
                onChange={(event) =>
                  setValues((prev) => ({ ...prev, github: { ...prev.github, app_id_ref: event.target.value } }))
                }
                placeholder="secret/app-id"
              />
            </div>
            <div className="space-y-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Private Key Ref</label>
              <Input
                value={values.github.private_key_ref}
                onChange={(event) =>
                  setValues((prev) => ({ ...prev, github: { ...prev.github, private_key_ref: event.target.value } }))
                }
                placeholder="secret/private-key"
              />
            </div>
          </div>
        ) : null}

        {step === 3 ? (
          <div className="space-y-3">
            <div className="space-y-2">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Allowlist (one URL per line)</label>
              <Textarea
                className="min-h-[120px]"
                value={textFields.allowlistText}
                onChange={(event) => setTextFields((prev) => ({ ...prev, allowlistText: event.target.value }))}
                placeholder="https://github.com/example/repo"
              />
            </div>
            <div className="grid gap-3 md:grid-cols-3">
              <div className="space-y-2">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Max runtime</label>
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
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Max loops</label>
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
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Max concurrency</label>
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
          </div>
        ) : null}

        {step === 4 ? (
          <div className="space-y-3 text-sm">
            <p className="rounded-md border bg-muted/30 p-3">
              <strong>Tenant:</strong> {values.tenantId || "-"} ({values.name || "-"})
            </p>
            <p className="rounded-md border bg-muted/30 p-3">
              <strong>Jira:</strong> {values.jira.mcp_endpoint || "-"} | keys: {textFields.projectKeysText || "-"}
            </p>
            <p className="rounded-md border bg-muted/30 p-3">
              <strong>GitHub refs:</strong> {values.github.app_id_ref || "-"}, {values.github.private_key_ref || "-"}
            </p>
            <p className="rounded-md border bg-muted/30 p-3">
              <strong>Allowlist entries:</strong> {textFields.allowlistText.split("\n").filter(Boolean).length}
            </p>
            <Button onClick={() => void handleCreateTenant()} disabled={saving}>
              {saving ? "Creating..." : "Create Tenant"}
            </Button>
          </div>
        ) : null}

        {step === 5 ? (
          <div className="space-y-3">
            <p className="rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
              <CheckCircle2 className="mr-1 inline h-4 w-4" />
              Tenant <strong>{createdTenantId || values.tenantId}</strong> is ready for GitHub App connection.
            </p>
            <Button onClick={() => void handleStartInstall()}>
              <Link2 className="mr-2 h-4 w-4" />
              Install GitHub App
            </Button>
            <Button variant="outline" asChild>
              <Link href={`/tenants/${encodeURIComponent(createdTenantId || values.tenantId)}/edit`}>
                Open Tenant Editor
              </Link>
            </Button>
          </div>
        ) : null}

        <div className="flex items-center justify-between gap-2 border-t pt-3">
          <Button variant="outline" onClick={previousStep} disabled={step === 0}>
            <ArrowLeft className="mr-2 h-4 w-4" /> Back
          </Button>
          <p className="text-xs text-muted-foreground">{statusLine}</p>
          <Button onClick={nextStep} disabled={step >= 4 || !canAdvance}>
            Next <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
