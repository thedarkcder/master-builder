"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast-provider";
import {
  getProjectInstallRequests,
  updateProjectInstallRequest,
  type Credentials,
  type ProjectInstallRequestRecord,
} from "@/lib/api";

type ProjectInstallsContentProps = {
  tenantId: string;
  projectId: string;
  credentials: Credentials | null;
};

const PLUGIN_NAMES: Record<string, string> = {
  atlassian: "Atlassian",
  fastlane: "Fastlane",
  github: "GitHub",
  hubspot: "HubSpot",
  jira: "Jira",
  railway: "Railway",
  slack: "Slack",
  stripe: "Stripe",
  supabase: "Supabase",
};

function pluginTokens(label: string): string[] {
  return label
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .split(" ")
    .filter(Boolean);
}

function pluginKey(label: string): string {
  const normalized = label.trim().toLowerCase();
  const tokens = pluginTokens(label);
  for (const key of Object.keys(PLUGIN_NAMES)) {
    if (normalized === key || tokens.includes(key)) {
      return key;
    }
  }
  return normalized.replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function pluginName(label: string): string {
  const normalized = label.trim().toLowerCase();
  if (PLUGIN_NAMES[normalized]) {
    return PLUGIN_NAMES[normalized];
  }
  const tokens = pluginTokens(label);
  for (const [key, name] of Object.entries(PLUGIN_NAMES)) {
    if (tokens.includes(key)) {
      return name;
    }
  }
  return label
    .trim()
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function pluginUseText(label: string): string {
  const key = pluginKey(label);
  const knownUses: Record<string, string> = {
    atlassian: "Use Atlassian for Jira issues, planning, and delivery status.",
    fastlane: "Use Fastlane for app build and release automation.",
    github: "Use GitHub for source code, branches, pull requests, and reviews.",
    hubspot: "Use HubSpot for customer, company, invoice, and subscription data.",
    jira: "Use Jira for issues, planning, and delivery status.",
    railway: "Use Railway for application deployment and environment management.",
    slack: "Use Slack for team notifications and workflow updates.",
    stripe: "Use Stripe for billing, invoices, payments, and subscriptions.",
    supabase: "Use Supabase for database and backend project operations.",
  };
  return knownUses[key] ?? `Use ${pluginName(label)} with this project.`;
}

function requestTime(request: ProjectInstallRequestRecord): number {
  const timestamp = Date.parse(request.updated_at || request.created_at || "");
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function statusPriority(status: string): number {
  const normalized = status.trim().toLowerCase();
  if (normalized === "pending") {
    return 3;
  }
  if (normalized === "approved") {
    return 2;
  }
  if (normalized === "fulfilled") {
    return 1;
  }
  return 0;
}

function visiblePluginRequests(requests: ProjectInstallRequestRecord[]): ProjectInstallRequestRecord[] {
  const byPlugin = new Map<string, ProjectInstallRequestRecord>();
  for (const request of requests) {
    const key = pluginKey(request.label);
    const existing = byPlugin.get(key);
    if (!existing) {
      byPlugin.set(key, request);
      continue;
    }
    const existingPriority = statusPriority(existing.status);
    const requestPriority = statusPriority(request.status);
    if (requestPriority > existingPriority) {
      byPlugin.set(key, request);
      continue;
    }
    if (requestPriority < existingPriority) {
      continue;
    }
    if (requestTime(request) > requestTime(existing)) {
      byPlugin.set(key, request);
    }
  }
  return Array.from(byPlugin.values()).sort((left, right) => requestTime(right) - requestTime(left));
}

export function ProjectInstallsContent({
  tenantId,
  projectId,
  credentials,
}: ProjectInstallsContentProps) {
  const { showToast } = useToast();
  const [requests, setRequests] = useState<ProjectInstallRequestRecord[]>([]);
  const [busyRequestId, setBusyRequestId] = useState<string | null>(null);
  const [statusLine, setStatusLine] = useState("");

  async function loadData() {
    if (!credentials) {
      return;
    }
    const response = await getProjectInstallRequests(credentials, tenantId, projectId);
    setRequests(response.requests ?? []);
  }

  useEffect(() => {
    if (!credentials) {
      return;
    }
    void loadData().catch((error) => {
      setStatusLine(`Failed to load plugin approvals: ${(error as Error).message}`);
    });
  }, [credentials, tenantId, projectId]);

  async function updateRequestStatus(request: ProjectInstallRequestRecord, status: "approved" | "rejected") {
    if (!credentials) {
      return;
    }
    const name = pluginName(request.label);
    setBusyRequestId(request.request_id);
    try {
      await updateProjectInstallRequest(credentials, tenantId, projectId, request.request_id, { status });
      showToast({
        title: status === "approved" ? `${name} plugin approved` : `${name} plugin declined`,
        tone: status === "approved" ? "success" : "info",
      });
      await loadData();
    } catch (error) {
      showToast({ title: "Plugin approval failed", description: (error as Error).message, tone: "error" });
    } finally {
      setBusyRequestId(null);
    }
  }

  return (
    <div className="space-y-4">
      {statusLine ? (
        <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p>
      ) : null}

      {visiblePluginRequests(requests).length === 0 ? (
        <div className="rounded-2xl border border-dashed bg-background px-6 py-8 text-sm text-muted-foreground">
          No plugin approvals are waiting for this project.
        </div>
      ) : (
        visiblePluginRequests(requests).map((request) => {
          const name = pluginName(request.label);
          const isFinal = request.status === "approved" || request.status === "rejected";
          const isBusy = busyRequestId === request.request_id;
          return (
            <div key={request.request_id} className="rounded-2xl border bg-background p-5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="text-base font-semibold">{name}</p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    {pluginUseText(request.label)}
                  </p>
                </div>
                <div className="flex gap-2">
                  <Button
                    disabled={isBusy || isFinal}
                    onClick={() => void updateRequestStatus(request, "approved")}
                    type="button"
                  >
                    {request.status === "approved" ? "Approved" : `Approve ${name}`}
                  </Button>
                  {request.status !== "approved" ? (
                    <Button
                      disabled={isBusy || isFinal}
                      onClick={() => void updateRequestStatus(request, "rejected")}
                      type="button"
                      variant="outline"
                    >
                      Decline
                    </Button>
                  ) : null}
                </div>
              </div>
            </div>
          );
        })
      )}
    </div>
  );
}
