"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/toast-provider";
import {
  completeProjectDeploymentSetup,
  getProjectDeploymentPolicy,
  listProjectGitHubBranches,
  updateProjectDeploymentPolicy,
  type ProjectDeploymentPolicyRecord,
  type ProjectGitHubBranchRecord,
} from "@/lib/api/deployments";

type ProjectDeploymentPolicyPageProps = {
  tenantId: string;
  projectId: string;
};

type PolicyFormState = {
  enabled: boolean;
  production_branch: string;
  preview_prs_enabled: boolean;
  generated_domain_policy: "disabled" | "production" | "production_and_preview";
  provider: ProjectDeploymentPolicyRecord["provider"];
  deployment_host_id: string | null;
  branch_settings: ProjectDeploymentPolicyRecord["branch_settings"];
  services: ProjectDeploymentPolicyRecord["services"];
  resources: ProjectDeploymentPolicyRecord["resources"];
  volumes: ProjectDeploymentPolicyRecord["volumes"];
};

const setupSteps = [
  {
    title: "Enable deployments",
  },
  {
    title: "Production branch",
  },
  {
    title: "Generated URLs",
  },
  {
    title: "Environment",
  },
  {
    title: "Finish",
  },
] as const;

type SetupStepIndex = 0 | 1 | 2 | 3 | 4;

function emptyPolicyForm(): PolicyFormState {
  return {
    enabled: false,
    production_branch: "",
    preview_prs_enabled: false,
    generated_domain_policy: "production",
    provider: "internal_coolify",
    deployment_host_id: null,
    branch_settings: {},
    services: [],
    resources: [],
    volumes: [],
  };
}

function toForm(policy: ProjectDeploymentPolicyRecord | null): PolicyFormState {
  if (!policy) return emptyPolicyForm();
  return {
    enabled: policy.enabled === true,
    production_branch: policy.production_branch ?? "",
    preview_prs_enabled: policy.preview_prs_enabled === true,
    generated_domain_policy: policy.generated_domain_policy,
    provider: policy.provider,
    deployment_host_id: policy.deployment_host_id,
    branch_settings: policy.branch_settings ?? {},
    services: policy.services ?? [],
    resources: policy.resources ?? [],
    volumes: policy.volumes ?? [],
  };
}

function branchSettingsFor(form: PolicyFormState, branch: string) {
  return form.branch_settings[branch] ?? { environment: {}, secret_refs: {} };
}

function setBranchEnvironmentValue(
  form: PolicyFormState,
  branch: string,
  key: string,
  value: string,
): PolicyFormState {
  const current = branchSettingsFor(form, branch);
  return {
    ...form,
    branch_settings: {
      ...form.branch_settings,
      [branch]: {
        ...current,
        environment: {
          ...current.environment,
          [key]: value,
        },
      },
    },
  };
}

function setBranchSecretRefValue(
  form: PolicyFormState,
  branch: string,
  key: string,
  value: string,
): PolicyFormState {
  const current = branchSettingsFor(form, branch);
  return {
    ...form,
    branch_settings: {
      ...form.branch_settings,
      [branch]: {
        ...current,
        secret_refs: {
          ...current.secret_refs,
          [key]: value,
        },
      },
    },
  };
}

function renameEnvironmentKey(form: PolicyFormState, branch: string, oldKey: string, newKey: string): PolicyFormState {
  const current = branchSettingsFor(form, branch);
  const { [oldKey]: value, ...remaining } = current.environment;
  return {
    ...form,
    branch_settings: {
      ...form.branch_settings,
      [branch]: {
        ...current,
        environment: {
          ...remaining,
          [newKey]: value ?? "",
        },
      },
    },
  };
}

function renameSecretRefKey(form: PolicyFormState, branch: string, oldKey: string, newKey: string): PolicyFormState {
  const current = branchSettingsFor(form, branch);
  const { [oldKey]: value, ...remaining } = current.secret_refs;
  return {
    ...form,
    branch_settings: {
      ...form.branch_settings,
      [branch]: {
        ...current,
        secret_refs: {
          ...remaining,
          [newKey]: value ?? "",
        },
      },
    },
  };
}

export function ProjectDeploymentPolicyPage({ tenantId, projectId }: ProjectDeploymentPolicyPageProps) {
  const { credentials, ready } = useAuth();
  const router = useRouter();
  const { showToast } = useToast();
  const [form, setForm] = useState<PolicyFormState>(() => emptyPolicyForm());
  const [activeStep, setActiveStep] = useState<SetupStepIndex>(0);
  const [branches, setBranches] = useState<ProjectGitHubBranchRecord[]>([]);
  const [policyConfigured, setPolicyConfigured] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [branchLoadError, setBranchLoadError] = useState("");

  useEffect(() => {
    if (!ready || !credentials) {
      setLoading(!ready);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError("");
    setBranchLoadError("");
    void Promise.all([
      getProjectDeploymentPolicy(credentials, tenantId, projectId),
      listProjectGitHubBranches(credentials, tenantId, projectId),
    ])
      .then(([policy, branchRecords]) => {
        if (cancelled) return;
        setForm(toForm(policy));
        setPolicyConfigured(Boolean(policy.enabled || policy.production_branch));
        setBranches(branchRecords);
      })
      .catch((loadError) => {
        if (cancelled) return;
        setError(`Failed to load deployment setup: ${(loadError as Error).message}`);
        setBranchLoadError((loadError as Error).message);
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [credentials, projectId, ready, tenantId]);

  function buildPolicyPayload() {
    return {
      enabled: form.enabled,
      production_branch: form.production_branch.trim() || null,
      preview_prs_enabled: form.preview_prs_enabled,
      provider: form.provider,
      deployment_host_id: form.deployment_host_id,
      generated_domain_policy: form.generated_domain_policy,
      branch_settings: form.branch_settings,
      services: form.services,
      resources: form.resources,
      volumes: form.volumes,
    };
  }

  async function savePolicy({ runSetup }: { runSetup: boolean }) {
    if (!credentials || saving) return;
    if (form.enabled && !form.production_branch.trim()) {
      setError("Choose the production branch before enabling deployments.");
      return;
    }
    if (form.enabled && branchLoadError) {
      setError("Branches must load before deployments can be enabled.");
      return;
    }
    if (form.preview_prs_enabled) {
      setError("Preview PR deployments are not available until the GitHub deployment worker supports PR release events.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const setupPayload = buildPolicyPayload();
      const saved = runSetup && form.enabled
        ? (await completeProjectDeploymentSetup(credentials, tenantId, projectId, setupPayload)).policy
        : await updateProjectDeploymentPolicy(credentials, tenantId, projectId, setupPayload);
      setForm(toForm(saved));
      setPolicyConfigured(Boolean(saved.enabled || saved.production_branch));
      showToast({
        title: runSetup && saved.enabled ? "Deployment setup started" : "Deployment policy saved",
        description: runSetup && saved.enabled
          ? `MB is setting up ${saved.production_branch} and creating the first release.`
          : "Project deployment settings were updated.",
      });
      if (runSetup) {
        router.push(`/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/deployments`);
      }
    } catch (saveError) {
      setError(`Failed to save deployment policy: ${(saveError as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return <main className="p-8 text-sm text-muted-foreground">Loading deployment setup...</main>;
  }

  const selectedBranchExists = branches.some((branch) => branch.name === form.production_branch);
  const branchOptions =
    form.production_branch && !selectedBranchExists
      ? [{ name: form.production_branch, protected: false }, ...branches]
      : branches;
  const canMoveNext =
    activeStep === 0
      ? true
      : activeStep === 1
        ? !form.enabled || (Boolean(form.production_branch.trim()) && !branchLoadError)
        : activeStep === 3
          ? Boolean(form.production_branch.trim())
          : true;

  function nextStep() {
    if (!canMoveNext) {
      setError(branchLoadError ? "Branches must load before deployments can be enabled." : "Choose the production branch.");
      return;
    }
    setError("");
    if (activeStep === 0 && !form.enabled) {
      setActiveStep(4);
      return;
    }
    setActiveStep((current) => (current < 4 ? ((current + 1) as SetupStepIndex) : current));
  }

  const activeBranch = form.production_branch.trim();
  const activeBranchSettings = activeBranch ? branchSettingsFor(form, activeBranch) : { environment: {}, secret_refs: {} };
  const environmentRows = Object.entries(activeBranchSettings.environment);
  const secretRefRows = Object.entries(activeBranchSettings.secret_refs);

  function previousStep() {
    setError("");
    setActiveStep((current) => (current > 0 ? ((current - 1) as SetupStepIndex) : current));
  }

  if (policyConfigured) {
    return (
      <main className="mx-auto max-w-5xl p-6">
        <div className="mb-8 flex flex-wrap items-center justify-between gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">Deployment policy</h1>
          <Button onClick={() => void savePolicy({ runSetup: false })} disabled={saving}>
            {saving ? "Saving..." : "Save policy"}
          </Button>
        </div>

        {error ? (
          <p className="mb-6 rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
            {error}
          </p>
        ) : null}

        <div className="space-y-6">
          <section className="rounded-2xl border bg-background p-6">
            <div className="grid gap-5 md:grid-cols-2">
              <fieldset className="space-y-3">
                <legend className="text-sm font-semibold">Release automation</legend>
                <label className="flex cursor-pointer items-center justify-between rounded-xl border px-4 py-3">
                  <span className="text-sm">Enabled</span>
                  <input
                    type="radio"
                    name="policy-enabled"
                    checked={form.enabled}
                    onChange={() => setForm((current) => ({ ...current, enabled: true }))}
                    className="h-4 w-4"
                  />
                </label>
                <label className="flex cursor-pointer items-center justify-between rounded-xl border px-4 py-3">
                  <span className="text-sm">Disabled</span>
                  <input
                    type="radio"
                    name="policy-enabled"
                    checked={!form.enabled}
                    onChange={() => setForm((current) => ({ ...current, enabled: false }))}
                    className="h-4 w-4"
                  />
                </label>
              </fieldset>

              <div className="space-y-5">
                <label className="block space-y-1.5">
                  <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Production branch</span>
                  <select
                    aria-label="Production branch"
                    className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                    value={form.production_branch}
                    onChange={(event) => setForm((current) => ({ ...current, production_branch: event.target.value }))}
                    disabled={saving || branchOptions.length === 0}
                  >
                    <option value="">Select a branch</option>
                    {branchOptions.map((branch) => (
                      <option key={branch.name} value={branch.name}>
                        {branch.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block space-y-1.5">
                  <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Generated URLs</span>
                  <select
                    aria-label="Generated URLs"
                    className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                    value={form.generated_domain_policy}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        generated_domain_policy: event.target.value as PolicyFormState["generated_domain_policy"],
                      }))
                    }
                    disabled={saving}
                  >
                    <option value="production">Production releases</option>
                    <option value="production_and_preview">Production and preview releases</option>
                    <option value="disabled">Do not generate URLs</option>
                  </select>
                </label>
              </div>
            </div>
          </section>

          <section className="rounded-2xl border bg-background p-6">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold">Environment</h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  Branch-scoped values used when MB creates deployment releases.
                </p>
              </div>
              <span className="rounded-full border px-3 py-1 text-xs text-muted-foreground">
                {activeBranch || "No branch selected"}
              </span>
            </div>

            {!activeBranch ? (
              <p className="mt-6 rounded-xl border px-4 py-3 text-sm text-muted-foreground">
                Choose a production branch before adding environment settings.
              </p>
            ) : (
              <div className="mt-6 grid gap-8 lg:grid-cols-2">
                <div className="space-y-3">
                  <div className="flex items-center justify-between gap-4">
                    <h3 className="text-sm font-semibold">Variables</h3>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        setForm((current) =>
                          setBranchEnvironmentValue(current, activeBranch, `ENV_${environmentRows.length + 1}`, ""),
                        )
                      }
                      disabled={saving}
                    >
                      Add variable
                    </Button>
                  </div>
                  {environmentRows.length === 0 ? (
                    <p className="rounded-xl border border-dashed px-4 py-4 text-sm text-muted-foreground">
                      No environment variables configured.
                    </p>
                  ) : (
                    environmentRows.map(([key, value]) => (
                      <div key={key} className="grid gap-2 md:grid-cols-[minmax(0,0.45fr)_minmax(0,1fr)]">
                        <Input
                          value={key}
                          onChange={(event) => setForm((current) => renameEnvironmentKey(current, activeBranch, key, event.target.value))}
                          aria-label={`Environment variable ${key} name`}
                          placeholder="APP_MODE"
                          disabled={saving}
                        />
                        <Input
                          value={value}
                          onChange={(event) => setForm((current) => setBranchEnvironmentValue(current, activeBranch, key, event.target.value))}
                          aria-label={`Environment variable ${key} value`}
                          placeholder="production"
                          disabled={saving}
                        />
                      </div>
                    ))
                  )}
                </div>

                <div className="space-y-3">
                  <div className="flex items-center justify-between gap-4">
                    <h3 className="text-sm font-semibold">Secret refs</h3>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        setForm((current) =>
                          setBranchSecretRefValue(current, activeBranch, `SECRET_${secretRefRows.length + 1}`, ""),
                        )
                      }
                      disabled={saving}
                    >
                      Add secret ref
                    </Button>
                  </div>
                  {secretRefRows.length === 0 ? (
                    <p className="rounded-xl border border-dashed px-4 py-4 text-sm text-muted-foreground">
                      No secret refs configured.
                    </p>
                  ) : (
                    secretRefRows.map(([key, value]) => (
                      <div key={key} className="grid gap-2 md:grid-cols-[minmax(0,0.45fr)_minmax(0,1fr)]">
                        <Input
                          value={key}
                          onChange={(event) => setForm((current) => renameSecretRefKey(current, activeBranch, key, event.target.value))}
                          aria-label={`Secret ref ${key} name`}
                          placeholder="DATABASE_URL"
                          disabled={saving}
                        />
                        <Input
                          value={value}
                          onChange={(event) => setForm((current) => setBranchSecretRefValue(current, activeBranch, key, event.target.value))}
                          aria-label={`Secret ref ${key} value`}
                          placeholder={`project/${projectId}/SECRET_NAME`}
                          disabled={saving}
                        />
                      </div>
                    ))
                  )}
                </div>
              </div>
            )}
          </section>
        </div>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-5xl p-6">
      <div className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">Deployments</h1>
      </div>

      <div className="grid gap-8 lg:grid-cols-[280px_minmax(0,1fr)]">
        <aside className="space-y-3" aria-label="Deployment setup steps">
          {setupSteps.map((step, index) => (
            <div
              key={step.title}
              className={`rounded-2xl border px-4 py-4 ${
                activeStep === index ? "border-primary bg-primary/5" : "bg-background"
              }`}
            >
              <div className="flex items-center gap-3">
                <span className="flex h-7 w-7 items-center justify-center rounded-full bg-primary/10 text-sm font-semibold text-primary">
                  {index + 1}
                </span>
                <h2 className="text-sm font-semibold">{step.title}</h2>
              </div>
            </div>
          ))}
        </aside>

        <section className="space-y-6">
          <div className="min-h-[320px] rounded-2xl border bg-background p-6">
            {activeStep === 0 ? (
              <div className="max-w-2xl">
                <h2 className="text-lg font-semibold">Enable project deployments</h2>
                <p className="mt-2 text-sm leading-6 text-muted-foreground">
                  When enabled, MB creates releases from approved GitHub events for this project.
                </p>
                <fieldset className="mt-8 space-y-3">
                  <legend className="sr-only">Deployment status</legend>
                  <label className="flex cursor-pointer items-center justify-between rounded-2xl border px-4 py-4">
                    <span>
                      <span className="block text-sm font-medium">Enabled</span>
                      <span className="mt-1 block text-sm text-muted-foreground">GitHub events can create releases.</span>
                    </span>
                    <input
                      type="radio"
                      name="deployment-enabled"
                      checked={form.enabled}
                      onChange={() => setForm((current) => ({ ...current, enabled: true }))}
                      className="h-4 w-4"
                    />
                  </label>
                  <label className="flex cursor-pointer items-center justify-between rounded-2xl border px-4 py-4">
                    <span>
                      <span className="block text-sm font-medium">Disabled</span>
                      <span className="mt-1 block text-sm text-muted-foreground">GitHub events will not create releases.</span>
                    </span>
                    <input
                      type="radio"
                      name="deployment-enabled"
                      checked={!form.enabled}
                      onChange={() => setForm((current) => ({ ...current, enabled: false }))}
                      className="h-4 w-4"
                    />
                  </label>
                </fieldset>
              </div>
            ) : null}

            {activeStep === 1 ? (
              <div className="max-w-md">
                <h2 className="text-lg font-semibold">Production branch</h2>
                <p className="mt-2 text-sm leading-6 text-muted-foreground">
                  Pushes to this branch create production deployment releases.
                </p>
                <label className="mt-8 block space-y-1.5">
                  <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Branch</span>
                  <select
                    aria-label="Production branch"
                    className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                    value={form.production_branch}
                    onChange={(event) => setForm((current) => ({ ...current, production_branch: event.target.value }))}
                    disabled={!form.enabled || saving || branchOptions.length === 0}
                  >
                    <option value="">Select a branch</option>
                    {branchOptions.map((branch) => (
                      <option key={branch.name} value={branch.name}>
                        {branch.name}
                      </option>
                    ))}
                  </select>
                </label>
                {!form.enabled ? (
                  <p className="mt-3 text-sm text-muted-foreground">Branch selection is skipped while deployments are disabled.</p>
                ) : null}
                {branchLoadError ? (
                  <p className="mt-3 text-sm text-destructive">Branches could not be loaded: {branchLoadError}</p>
                ) : null}
              </div>
            ) : null}

            {activeStep === 2 ? (
              <div className="max-w-md">
                <h2 className="text-lg font-semibold">Generated URLs</h2>
                <p className="mt-2 text-sm leading-6 text-muted-foreground">
                  Choose when MB should create generated public URLs for releases.
                </p>
                <label className="mt-8 block space-y-1.5">
                  <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Generated URLs</span>
                  <select
                    className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                    value={form.generated_domain_policy}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        generated_domain_policy: event.target.value as PolicyFormState["generated_domain_policy"],
                      }))
                    }
                    disabled={saving}
                  >
                    <option value="production">Production releases</option>
                    <option value="production_and_preview">Production and preview releases</option>
                    <option value="disabled">Do not generate URLs</option>
                  </select>
                </label>
              </div>
            ) : null}

            {activeStep === 3 ? (
              <div className="max-w-2xl">
                <h2 className="text-lg font-semibold">Environment</h2>
                <p className="mt-2 text-sm leading-6 text-muted-foreground">
                  Add values for the {activeBranch || "selected"} deployment branch. Use secret refs for credentials.
                </p>
                {!activeBranch ? (
                  <p className="mt-8 rounded-xl border px-4 py-3 text-sm text-muted-foreground">
                    Choose a production branch before adding environment settings.
                  </p>
                ) : (
                  <div className="mt-8 space-y-8">
                    <div className="space-y-3">
                      <div className="flex items-center justify-between gap-4">
                        <h3 className="text-sm font-semibold">Environment variables</h3>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() =>
                            setForm((current) =>
                              setBranchEnvironmentValue(
                                current,
                                activeBranch,
                                `ENV_${environmentRows.length + 1}`,
                                "",
                              ),
                            )
                          }
                          disabled={!activeBranch || saving}
                        >
                          Add variable
                        </Button>
                      </div>
                      {environmentRows.length === 0 ? (
                        <p className="rounded-xl border border-dashed px-4 py-4 text-sm text-muted-foreground">
                          No environment variables configured for {activeBranch}.
                        </p>
                      ) : (
                        environmentRows.map(([key, value]) => (
                          <div key={key} className="grid gap-2 md:grid-cols-[minmax(0,0.4fr)_minmax(0,1fr)_auto]">
                            <Input
                              value={key}
                              onChange={(event) =>
                                setForm((current) => renameEnvironmentKey(current, activeBranch, key, event.target.value))
                              }
                              aria-label={`Environment variable ${key} name`}
                              placeholder="APP_MODE"
                              disabled={saving}
                            />
                            <Input
                              value={value}
                              onChange={(event) =>
                                setForm((current) => setBranchEnvironmentValue(current, activeBranch, key, event.target.value))
                              }
                              aria-label={`Environment variable ${key} value`}
                              placeholder="Value"
                              disabled={saving}
                            />
                            <Button
                              type="button"
                              variant="outline"
                              onClick={() =>
                                setForm((current) => {
                                  const settings = branchSettingsFor(current, activeBranch);
                                  const { [key]: _removed, ...nextEnvironment } = settings.environment;
                                  return {
                                    ...current,
                                    branch_settings: {
                                      ...current.branch_settings,
                                      [activeBranch]: { ...settings, environment: nextEnvironment },
                                    },
                                  };
                                })
                              }
                              disabled={saving}
                            >
                              Remove
                            </Button>
                          </div>
                        ))
                      )}
                    </div>

                    <div className="space-y-3">
                      <div className="flex items-center justify-between gap-4">
                        <h3 className="text-sm font-semibold">Secret refs</h3>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() =>
                            setForm((current) =>
                              setBranchSecretRefValue(
                                current,
                                activeBranch,
                                `SECRET_${secretRefRows.length + 1}`,
                                "",
                              ),
                            )
                          }
                          disabled={!activeBranch || saving}
                        >
                          Add secret ref
                        </Button>
                      </div>
                      {secretRefRows.length === 0 ? (
                        <p className="rounded-xl border border-dashed px-4 py-4 text-sm text-muted-foreground">
                          No secret refs configured for {activeBranch}.
                        </p>
                      ) : (
                        secretRefRows.map(([key, value]) => (
                          <div key={key} className="grid gap-2 md:grid-cols-[minmax(0,0.4fr)_minmax(0,1fr)_auto]">
                            <Input
                              value={key}
                              onChange={(event) =>
                                setForm((current) => renameSecretRefKey(current, activeBranch, key, event.target.value))
                              }
                              aria-label={`Secret ref ${key} name`}
                              placeholder="DATABASE_URL"
                              disabled={saving}
                            />
                            <Input
                              value={value}
                              onChange={(event) =>
                                setForm((current) => setBranchSecretRefValue(current, activeBranch, key, event.target.value))
                              }
                              aria-label={`Secret ref ${key} value`}
                              placeholder={`project/${projectId}/SECRET_NAME`}
                              disabled={saving}
                            />
                            <Button
                              type="button"
                              variant="outline"
                              onClick={() =>
                                setForm((current) => {
                                  const settings = branchSettingsFor(current, activeBranch);
                                  const { [key]: _removed, ...nextSecretRefs } = settings.secret_refs;
                                  return {
                                    ...current,
                                    branch_settings: {
                                      ...current.branch_settings,
                                      [activeBranch]: { ...settings, secret_refs: nextSecretRefs },
                                    },
                                  };
                                })
                              }
                              disabled={saving}
                            >
                              Remove
                            </Button>
                          </div>
                        ))
                      )}
                    </div>
                  </div>
                )}
              </div>
            ) : null}

            {activeStep === 4 ? (
              <div className="max-w-2xl">
                <h2 className="text-lg font-semibold">Finish</h2>
                <dl className="mt-8 grid gap-4 text-sm md:grid-cols-2">
                  <div className="rounded-xl border px-4 py-3">
                    <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Deployments</dt>
                    <dd className="mt-2">{form.enabled ? "Enabled" : "Disabled"}</dd>
                  </div>
                  <div className="rounded-xl border px-4 py-3">
                    <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Production branch</dt>
                    <dd className="mt-2">{form.enabled ? activeBranch : "-"}</dd>
                  </div>
                  <div className="rounded-xl border px-4 py-3">
                    <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Generated URLs</dt>
                    <dd className="mt-2">{form.generated_domain_policy}</dd>
                  </div>
                  <div className="rounded-xl border px-4 py-3">
                    <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Branch settings</dt>
                    <dd className="mt-2">
                      {environmentRows.length} variables, {secretRefRows.length} secret refs
                    </dd>
                  </div>
                </dl>
              </div>
            ) : null}
          </div>

          {error ? (
            <p className="rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
              {error}
            </p>
          ) : null}

          <div className="flex items-center justify-between">
            <Button type="button" variant="outline" onClick={previousStep} disabled={saving || activeStep === 0}>
              Back
            </Button>
            {activeStep < 4 ? (
              <Button type="button" onClick={nextStep} disabled={saving}>
                Next
              </Button>
            ) : (
              <Button onClick={() => void savePolicy({ runSetup: true })} disabled={saving}>
                {saving ? "Starting..." : "Finish setup"}
              </Button>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
