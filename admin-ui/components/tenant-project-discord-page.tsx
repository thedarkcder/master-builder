"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { DiscordSection } from "@/components/tenant-form-sections";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  approveDiscordAllowlistRequest,
  getProject,
  listDiscordAllowlistRequests,
  updateProject,
  type DiscordAllowlistRequestRecord,
  type ProjectRecord
} from "@/lib/api";

export function TenantProjectDiscordPage() {
  const params = useParams<{ tenantId: string; projectId: string }>();
  const { credentials, ready } = useAuth();

  const [project, setProject] = useState<ProjectRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [discordEnabled, setDiscordEnabled] = useState(false);
  const [notifyEvents, setNotifyEvents] = useState<string[]>([]);
  const [allowlistRequests, setAllowlistRequests] = useState<DiscordAllowlistRequestRecord[]>([]);
  const [allowlistBusyUserId, setAllowlistBusyUserId] = useState<string | null>(null);

  useEffect(() => {
    if (!ready || !credentials) {
      return;
    }
    void (async () => {
      setBusy(true);
      try {
        const payload = await getProject(credentials, params.tenantId, params.projectId);
        const requests = await listDiscordAllowlistRequests(credentials, params.tenantId, params.projectId);
        setProject(payload);
        setDiscordEnabled(Boolean(payload.discord));
        setNotifyEvents(payload.discord?.notify_events ?? []);
        setAllowlistRequests(requests);
        setStatusLine("");
      } catch (error) {
        setStatusLine(`Failed to load project: ${(error as Error).message}`);
      } finally {
        setBusy(false);
      }
    })();
  }, [ready, credentials, params.tenantId, params.projectId]);

  function toggleNotifyEvent(eventValue: string, enabled: boolean): void {
    setNotifyEvents((current) =>
      enabled ? Array.from(new Set([...current, eventValue])) : current.filter((value) => value !== eventValue)
    );
  }

  async function save() {
    if (!credentials || !project) {
      return;
    }
    setBusy(true);
    try {
      const updated = await updateProject(credentials, params.tenantId, params.projectId, {
        name: project.name,
        github_repository: project.github_repository,
        jira_project_key: project.jira_project_key,
        policy_overrides: project.policy_overrides,
        environment: project.environment,
        secret_refs: project.secret_refs,
        discord: discordEnabled
          ? {
              notify_events: notifyEvents,
            }
          : null,
        is_archived: project.is_archived,
      });
      setProject(updated);
      setDiscordEnabled(Boolean(updated.discord));
      setNotifyEvents(updated.discord?.notify_events ?? []);
      setStatusLine("Project Discord settings saved.");
    } catch (error) {
      setStatusLine(`Save failed: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function approveAllowlistRequest(userId: string) {
    if (!credentials) {
      return;
    }
    setAllowlistBusyUserId(userId);
    try {
      const result = await approveDiscordAllowlistRequest(credentials, params.tenantId, params.projectId, userId);
      const requests = await listDiscordAllowlistRequests(credentials, params.tenantId, params.projectId);
      setAllowlistRequests(requests);
      setStatusLine(result.details);
    } catch (error) {
      setStatusLine(`Approve failed: ${(error as Error).message}`);
    } finally {
      setAllowlistBusyUserId(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle>Project Discord</CardTitle>
            <CardDescription>{project?.name ?? params.projectId}</CardDescription>
          </div>
          <Button asChild variant="outline">
            <Link href={`/tenants/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(params.projectId)}`}>
              <ArrowLeft className="mr-2 h-4 w-4" />
              Back to Project
            </Link>
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {statusLine ? <p className="rounded-md border px-3 py-2 text-sm text-muted-foreground">{statusLine}</p> : null}
        <DiscordSection
          title="Project Discord"
          description="Enable notifications for this project and choose which events should be posted."
          discordEnabled={discordEnabled}
          notifyEvents={notifyEvents}
          onDiscordEnabledChange={setDiscordEnabled}
          onToggleDiscordNotifyEvent={toggleNotifyEvent}
        />
        <div>
          <Button onClick={() => void save()} disabled={busy || !project}>
            {busy ? "Saving..." : "Save"}
          </Button>
        </div>
        <div className="space-y-2 rounded-md border p-3 text-sm">
          <p className="font-medium">Access Requests</p>
          <p className="text-muted-foreground">Approve pending `/request` submissions for this project.</p>
          {allowlistRequests.length === 0 ? (
            <p className="text-muted-foreground">No pending requests.</p>
          ) : (
            <ul className="space-y-2">
              {allowlistRequests.map((request) => (
                <li key={request.user_id} className="rounded-md border p-3">
                  <p>
                    <strong>User:</strong> {request.user_id}
                  </p>
                  <p>
                    <strong>Requested at:</strong> {request.requested_at}
                  </p>
                  <p>
                    <strong>Reason:</strong> {request.reason ?? "-"}
                  </p>
                  <p>
                    <strong>Channel:</strong> {request.channel_id ?? "-"}
                  </p>
                  <div className="mt-2">
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={allowlistBusyUserId === request.user_id}
                      onClick={() => void approveAllowlistRequest(request.user_id)}
                    >
                      {allowlistBusyUserId === request.user_id ? "Approving..." : "Approve"}
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
