"use client";

import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/components/ui/toast-provider";
import {
  createProjectInstall,
  deleteProjectInstall,
  getProjectInstallRequests,
  getProjectInstalls,
  updateProjectInstall,
  updateProjectInstallRequest,
  type Credentials,
  type ProjectInstallPayload,
  type ProjectInstallRecord,
  type ProjectInstallRequestRecord,
} from "@/lib/api";

type ProjectInstallsContentProps = {
  tenantId: string;
  projectId: string;
  credentials: Credentials | null;
};

type InstallFormState = {
  installId: string | null;
  kind: string;
  label: string;
  enabled: boolean;
  bindingNamesText: string;
  configText: string;
};

function defaultFormState(): InstallFormState {
  return {
    installId: null,
    kind: "fastlane_lane",
    label: "",
    enabled: true,
    bindingNamesText: "",
    configText: "{\n  \"working_dir\": \".\"\n}",
  };
}

function parseBindingNames(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function buildPayload(form: InstallFormState): ProjectInstallPayload {
  let parsedConfig: Record<string, unknown> = {};
  try {
    parsedConfig = JSON.parse(form.configText);
  } catch (error) {
    throw new Error(`Config must be valid JSON: ${(error as Error).message}`);
  }
  if (!form.kind.trim()) {
    throw new Error("Install kind is required.");
  }
  if (!form.label.trim()) {
    throw new Error("Install label is required.");
  }
  return {
    kind: form.kind.trim(),
    label: form.label.trim(),
    enabled: form.enabled,
    binding_names: parseBindingNames(form.bindingNamesText),
    config: parsedConfig,
  };
}

function buildFormState(install: ProjectInstallRecord): InstallFormState {
  return {
    installId: install.install_id,
    kind: install.kind,
    label: install.label,
    enabled: install.enabled,
    bindingNamesText: (install.binding_names ?? []).join("\n"),
    configText: JSON.stringify(install.config ?? {}, null, 2),
  };
}

export function ProjectInstallsContent({
  tenantId,
  projectId,
  credentials,
}: ProjectInstallsContentProps) {
  const { showToast } = useToast();
  const [installs, setInstalls] = useState<ProjectInstallRecord[]>([]);
  const [requests, setRequests] = useState<ProjectInstallRequestRecord[]>([]);
  const [form, setForm] = useState<InstallFormState>(() => defaultFormState());
  const [busy, setBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("");

  async function loadData() {
    if (!credentials) {
      return;
    }
    const [installsResponse, requestsResponse] = await Promise.all([
      getProjectInstalls(credentials, tenantId, projectId),
      getProjectInstallRequests(credentials, tenantId, projectId),
    ]);
    setInstalls(installsResponse.installs ?? []);
    setRequests(requestsResponse.requests ?? []);
  }

  useEffect(() => {
    if (!credentials) {
      return;
    }
    void loadData().catch((error) => {
      setStatusLine(`Failed to load installs: ${(error as Error).message}`);
    });
  }, [credentials, tenantId, projectId]);

  const matchingRequestMap = useMemo(() => {
    return new Set(installs.map((install) => `${install.kind}::${install.label}`));
  }, [installs]);

  async function saveInstall() {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      const payload = buildPayload(form);
      if (form.installId) {
        await updateProjectInstall(credentials, tenantId, projectId, form.installId, payload);
        showToast({ title: "Install updated", description: payload.label, tone: "success" });
      } else {
        await createProjectInstall(credentials, tenantId, projectId, payload);
        showToast({ title: "Install created", description: payload.label, tone: "success" });
      }
      setForm(defaultFormState());
      await loadData();
    } catch (error) {
      showToast({ title: "Install save failed", description: (error as Error).message, tone: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function removeInstall(installId: string) {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      await deleteProjectInstall(credentials, tenantId, projectId, installId);
      if (form.installId === installId) {
        setForm(defaultFormState());
      }
      showToast({ title: "Install deleted", tone: "success" });
      await loadData();
    } catch (error) {
      showToast({ title: "Install delete failed", description: (error as Error).message, tone: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function updateRequestStatus(requestId: string, status: string) {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      await updateProjectInstallRequest(credentials, tenantId, projectId, requestId, { status });
      showToast({ title: "Install request updated", description: `Marked ${status}.`, tone: "success" });
      await loadData();
    } catch (error) {
      showToast({ title: "Install request update failed", description: (error as Error).message, tone: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      {statusLine ? (
        <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p>
      ) : null}

      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="border-b px-6 py-5">
          <h2 className="text-base font-semibold">Installed Integrations</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Register project-scoped installs and the bindings each install is allowed to receive at execution time.
          </p>
        </div>
        <div className="grid gap-6 p-6 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)]">
          <div className="space-y-4">
            <div className="grid gap-4 md:grid-cols-2">
              <label className="space-y-2">
                <span className="text-sm font-medium">Kind</span>
                <Input value={form.kind} onChange={(event) => setForm((current) => ({ ...current, kind: event.target.value }))} />
              </label>
              <label className="space-y-2">
                <span className="text-sm font-medium">Label</span>
                <Input value={form.label} onChange={(event) => setForm((current) => ({ ...current, label: event.target.value }))} />
              </label>
            </div>
            <label className="flex items-center gap-3 text-sm">
              <input
                checked={form.enabled}
                className="h-4 w-4"
                onChange={(event) => setForm((current) => ({ ...current, enabled: event.target.checked }))}
                type="checkbox"
              />
              Enabled
            </label>
            <label className="space-y-2">
              <span className="text-sm font-medium">Allowed bindings</span>
              <Textarea
                value={form.bindingNamesText}
                onChange={(event) => setForm((current) => ({ ...current, bindingNamesText: event.target.value }))}
                rows={4}
              />
              <p className="text-xs text-muted-foreground">One binding name per line or comma-separated.</p>
            </label>
            <label className="space-y-2">
              <span className="text-sm font-medium">Config JSON</span>
              <Textarea
                value={form.configText}
                onChange={(event) => setForm((current) => ({ ...current, configText: event.target.value }))}
                rows={12}
              />
            </label>
            <div className="flex flex-wrap gap-3">
              <Button disabled={busy} onClick={() => void saveInstall()}>
                {form.installId ? "Update install" : "Create install"}
              </Button>
              <Button
                disabled={busy}
                onClick={() => setForm(defaultFormState())}
                type="button"
                variant="outline"
              >
                Reset
              </Button>
            </div>
          </div>

          <div className="space-y-4">
            {installs.length === 0 ? (
              <div className="rounded-xl border border-dashed px-4 py-6 text-sm text-muted-foreground">
                No installs registered for this project yet.
              </div>
            ) : (
              installs.map((install) => (
                <div key={install.install_id} className="rounded-xl border p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="space-y-1">
                      <p className="text-sm font-medium">{install.label}</p>
                      <p className="text-xs text-muted-foreground">
                        {install.kind} · {install.enabled ? "enabled" : "disabled"}
                      </p>
                    </div>
                    <div className="flex gap-2">
                      <Button
                        disabled={busy}
                        onClick={() => setForm(buildFormState(install))}
                        size="sm"
                        type="button"
                        variant="outline"
                      >
                        Edit
                      </Button>
                      <Button
                        disabled={busy}
                        onClick={() => void removeInstall(install.install_id)}
                        size="sm"
                        type="button"
                        variant="outline"
                      >
                        Delete
                      </Button>
                    </div>
                  </div>
                  <div className="mt-3 space-y-2 text-xs text-muted-foreground">
                    <p>Bindings: {(install.binding_names ?? []).join(", ") || "None"}</p>
                    <pre className="overflow-x-auto rounded-md bg-muted/40 p-3 text-[11px]">
                      {JSON.stringify(install.config ?? {}, null, 2)}
                    </pre>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="border-b px-6 py-5">
          <h2 className="text-base font-semibold">Install Requests</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Runs create these when they need an integration that is missing or unsupported for the project.
          </p>
        </div>
        <div className="space-y-4 p-6">
          {requests.length === 0 ? (
            <div className="rounded-xl border border-dashed px-4 py-6 text-sm text-muted-foreground">
              No install requests yet.
            </div>
          ) : (
            requests.map((request) => {
              const hasMatchingInstall = matchingRequestMap.has(`${request.kind}::${request.label}`);
              return (
                <div key={request.request_id} className="rounded-xl border p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="space-y-1">
                      <p className="text-sm font-medium">
                        {request.label} · {request.kind}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {request.issue_key} · {request.request_kind} · {request.status}
                      </p>
                    </div>
                    <div className="flex gap-2">
                      <Button
                        disabled={busy || !hasMatchingInstall || request.status === "fulfilled"}
                        onClick={() => void updateRequestStatus(request.request_id, "fulfilled")}
                        size="sm"
                        type="button"
                        variant="outline"
                      >
                        Mark fulfilled
                      </Button>
                      <Button
                        disabled={busy || request.status === "rejected"}
                        onClick={() => void updateRequestStatus(request.request_id, "rejected")}
                        size="sm"
                        type="button"
                        variant="outline"
                      >
                        Reject
                      </Button>
                    </div>
                  </div>
                  <div className="mt-3 space-y-2 text-xs text-muted-foreground">
                    <p>{request.reason}</p>
                    <p>Required bindings: {(request.required_bindings ?? []).join(", ") || "None"}</p>
                    <pre className="overflow-x-auto rounded-md bg-muted/40 p-3 text-[11px]">
                      {JSON.stringify(request.suggested_config ?? {}, null, 2)}
                    </pre>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
