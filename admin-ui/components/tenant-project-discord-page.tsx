"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft, CheckCircle2, Clock, MessageSquare, User } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { DiscordSection } from "@/components/tenant-form-sections";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  approveDiscordAllowlistRequest,
  getProject,
  listDiscordAllowlistRequests,
  updateProject,
  type Credentials,
  type DiscordAllowlistRequestRecord,
  type ProjectRecord,
} from "@/lib/api";

// ─── Extracted content component (used as a tab in the project detail page) ────

type ProjectNotificationsContentProps = {
  tenantId: string;
  projectId: string;
  credentials: Credentials | null;
};

export function ProjectNotificationsContent({
  tenantId,
  projectId,
  credentials,
}: ProjectNotificationsContentProps) {
  const [project, setProject] = useState<ProjectRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [discordEnabled, setDiscordEnabled] = useState(false);
  const [notifyEvents, setNotifyEvents] = useState<string[]>([]);
  const [liveVoiceEnabled, setLiveVoiceEnabled] = useState(false);
  const [liveVoiceChannelId, setLiveVoiceChannelId] = useState("");
  const [linkedTextChannelId, setLinkedTextChannelId] = useState("");
  const [allowlistRequests, setAllowlistRequests] = useState<DiscordAllowlistRequestRecord[]>([]);
  const [allowlistBusyUserId, setAllowlistBusyUserId] = useState<string | null>(null);

  useEffect(() => {
    if (!credentials) return;
    void (async () => {
      setBusy(true);
      try {
        const payload = await getProject(credentials, tenantId, projectId);
        const requests = await listDiscordAllowlistRequests(credentials, tenantId, projectId);
        setProject(payload);
        setDiscordEnabled(Boolean(payload.discord));
        setNotifyEvents(payload.discord?.notify_events ?? []);
        setLiveVoiceEnabled(Boolean(payload.discord?.live_voice_enabled));
        setLiveVoiceChannelId(payload.discord?.live_voice_channel_id ?? "");
        setLinkedTextChannelId(payload.discord?.live_voice_linked_text_channel_id ?? "");
        setAllowlistRequests(requests);
        setStatusLine("");
      } catch (error) {
        setStatusLine(`Failed to load project: ${(error as Error).message}`);
      } finally {
        setBusy(false);
      }
    })();
  }, [credentials, tenantId, projectId]);

  function toggleNotifyEvent(eventValue: string, enabled: boolean): void {
    setNotifyEvents((current) =>
      enabled ? Array.from(new Set([...current, eventValue])) : current.filter((v) => v !== eventValue)
    );
  }

  async function save() {
    if (!credentials || !project) return;
    const normalizedLiveVoiceChannelId = liveVoiceChannelId.trim();
    const normalizedLinkedTextChannelId = linkedTextChannelId.trim();
    if (discordEnabled && liveVoiceEnabled && (!normalizedLiveVoiceChannelId || !normalizedLinkedTextChannelId)) {
      setStatusLine("Enter both the voice channel ID and linked text channel/thread ID.");
      return;
    }
    const liveVoiceRoomLinks =
      liveVoiceEnabled && normalizedLiveVoiceChannelId && normalizedLinkedTextChannelId
        ? { [normalizedLiveVoiceChannelId]: normalizedLinkedTextChannelId }
        : {};
    setBusy(true);
    try {
      const updated = await updateProject(credentials, tenantId, projectId, {
        name: project.name,
        github_repository: project.github_repository,
        jira_project_key: project.jira_project_key,
        policy_overrides: project.policy_overrides,
        environment: project.environment,
        secret_refs: project.secret_refs,
        discord: discordEnabled
          ? {
              ...(project.discord ?? {}),
              notify_events: notifyEvents,
              live_voice_enabled: liveVoiceEnabled,
              live_voice_channel_id: liveVoiceEnabled ? (normalizedLiveVoiceChannelId || null) : null,
              live_voice_linked_text_channel_id: liveVoiceEnabled ? (normalizedLinkedTextChannelId || null) : null,
              live_voice_room_links: liveVoiceRoomLinks,
            }
          : null,
        is_archived: project.is_archived,
      });
      setProject(updated);
      setDiscordEnabled(Boolean(updated.discord));
      setNotifyEvents(updated.discord?.notify_events ?? []);
      setLiveVoiceEnabled(Boolean(updated.discord?.live_voice_enabled));
      setLiveVoiceChannelId(updated.discord?.live_voice_channel_id ?? "");
      setLinkedTextChannelId(updated.discord?.live_voice_linked_text_channel_id ?? "");
      if (updated.discord?.live_voice_enabled) {
        const savedVoiceChannelId = updated.discord.live_voice_channel_id ?? normalizedLiveVoiceChannelId;
        const savedLinkedTextChannelId =
          updated.discord.live_voice_linked_text_channel_id ?? normalizedLinkedTextChannelId;
        setStatusLine(
          `Discord settings saved. Live voice room ${savedVoiceChannelId} is linked to ${savedLinkedTextChannelId}.`
        );
      } else {
        setStatusLine("Discord settings saved. Live voice is disabled for this project.");
      }
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
      const result = await approveDiscordAllowlistRequest(credentials, tenantId, projectId, userId);
      const requests = await listDiscordAllowlistRequests(credentials, tenantId, projectId);
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
          {discordEnabled ? (
            <div className="space-y-4 rounded-md border p-4">
              <div className="space-y-1">
                <p className="text-sm font-medium">Live Voice Room</p>
                <p className="text-xs text-muted-foreground">
                  Each project supports one live voice room. The linked text channel is where transcripts and persona replies are mirrored.
                </p>
              </div>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  className="h-4 w-4 rounded border-input"
                  checked={liveVoiceEnabled}
                  onChange={(event) => setLiveVoiceEnabled(event.target.checked)}
                />
                <span>Enable live voice rooms for this project</span>
              </label>
              {liveVoiceEnabled ? (
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="space-y-1">
                    <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Voice Channel ID</p>
                    <Input
                      value={liveVoiceChannelId}
                      onChange={(event) => setLiveVoiceChannelId(event.target.value)}
                      placeholder="123456789012345678"
                    />
                  </div>
                  <div className="space-y-1">
                    <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Linked Text Channel / Thread ID</p>
                    <Input
                      value={linkedTextChannelId}
                      onChange={(event) => setLinkedTextChannelId(event.target.value)}
                      placeholder="987654321098765432"
                    />
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}
          <div className="border-t pt-4">
            <Button type="button" size="sm" onClick={() => void save()} disabled={busy || !project}>
              {busy ? "Saving…" : "Save Discord settings"}
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
          <p className="text-sm text-muted-foreground">
            Approve pending{" "}
            <code className="rounded bg-muted px-1 text-xs">/request</code>{" "}
            submissions for this project.
          </p>
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
                    {request.reason ? <p className="text-sm">{request.reason}</p> : null}
                    <p className="flex items-center gap-1 text-xs text-muted-foreground">
                      <Clock className="h-3 w-3" />
                      {request.requested_at}
                    </p>
                  </div>
                  <Button
                    type="button"
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

// ─── Standalone page shell (for the /discord sub-route) ─────────────────────

export function TenantProjectDiscordPage() {
  const params = useParams<{ tenantId: string; projectId: string }>();
  const { credentials } = useAuth();

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold">
            <MessageSquare className="h-5 w-5 text-muted-foreground" />
            Discord
          </h1>
        </div>
        <Button asChild variant="outline" size="sm">
          <Link href={`/tenants/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(params.projectId)}`}>
            <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />
            Back to Project
          </Link>
        </Button>
      </div>

      <ProjectNotificationsContent
        tenantId={params.tenantId}
        projectId={params.projectId}
        credentials={credentials}
      />
    </div>
  );
}
