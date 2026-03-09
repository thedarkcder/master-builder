"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft, CheckCircle2, Clock, MessageSquare, User } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { DiscordSection } from "@/components/tenant-form-sections";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  approveDiscordAllowlistRequest,
  getProject,
  listDiscordAllowlistRequests,
  updateProject,
  type DiscordAllowlistRequestRecord,
  type ProjectRecord,
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
    if (!ready || !credentials) return;
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
      enabled ? Array.from(new Set([...current, eventValue])) : current.filter((v) => v !== eventValue)
    );
  }

  async function save() {
    if (!credentials || !project) return;
    setBusy(true);
    try {
      const updated = await updateProject(credentials, params.tenantId, params.projectId, {
        name: project.name,
        github_repository: project.github_repository,
        jira_project_key: project.jira_project_key,
        policy_overrides: project.policy_overrides,
        environment: project.environment,
        secret_refs: project.secret_refs,
        discord: discordEnabled ? { notify_events: notifyEvents } : null,
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
    if (!credentials) return;
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
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold">
            <MessageSquare className="h-5 w-5 text-muted-foreground" />
            Discord
          </h1>
          <p className="mt-0.5 text-sm text-muted-foreground">{project?.name ?? params.projectId}</p>
        </div>
        <Button asChild variant="outline" size="sm">
          <Link href={`/tenants/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(params.projectId)}`}>
            <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />
            Back to Project
          </Link>
        </Button>
      </div>

      {statusLine ? (
        <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p>
      ) : null}

      {/* Notification Settings */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Notification Settings</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <DiscordSection
            title="Project Discord"
            description="Enable notifications for this project and choose which events should be posted."
            discordEnabled={discordEnabled}
            notifyEvents={notifyEvents}
            onDiscordEnabledChange={setDiscordEnabled}
            onToggleDiscordNotifyEvent={toggleNotifyEvent}
          />
          <div className="border-t pt-4">
            <Button size="sm" onClick={() => void save()} disabled={busy || !project}>
              {busy ? "Saving…" : "Save settings"}
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Access Requests */}
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between gap-2">
            <CardTitle className="text-base">Access Requests</CardTitle>
            {allowlistRequests.length > 0 ? (
              <span className="flex h-5 w-5 items-center justify-center rounded-full bg-warning text-[10px] font-bold text-white">
                {allowlistRequests.length}
              </span>
            ) : null}
          </div>
          <p className="text-sm text-muted-foreground">Approve pending <code className="rounded bg-muted px-1 text-xs">/request</code> submissions for this project.</p>
        </CardHeader>
        <CardContent>
          {allowlistRequests.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-2 rounded-lg border bg-muted/30 py-8 text-center">
              <CheckCircle2 className="h-6 w-6 text-success" />
              <p className="text-sm font-medium">No pending requests</p>
              <p className="text-xs text-muted-foreground">All access requests have been handled.</p>
            </div>
          ) : (
            <ul className="space-y-3">
              {allowlistRequests.map((request) => (
                <li
                  key={request.user_id}
                  className="flex flex-wrap items-start justify-between gap-3 rounded-lg border bg-muted/20 p-4"
                >
                  <div className="space-y-1.5 min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="inline-flex items-center gap-1.5 rounded-full bg-muted px-2.5 py-0.5 text-xs font-medium">
                        <User className="h-3 w-3" />
                        {request.user_id}
                      </span>
                      {request.channel_id ? (
                        <span className="text-xs text-muted-foreground">#{request.channel_id}</span>
                      ) : null}
                    </div>
                    {request.reason ? (
                      <p className="text-sm">{request.reason}</p>
                    ) : null}
                    <p className="flex items-center gap-1 text-xs text-muted-foreground">
                      <Clock className="h-3 w-3" />
                      {request.requested_at}
                    </p>
                  </div>
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={allowlistBusyUserId === request.user_id}
                    onClick={() => void approveAllowlistRequest(request.user_id)}
                  >
                    {allowlistBusyUserId === request.user_id ? "Approving…" : "Approve"}
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
