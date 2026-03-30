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
  getProjectAutomations,
  getProject,
  listDiscordAllowlistRequests,
  type ProjectAutomationExecutionRecord,
  updateProjectAutomations,
  updateProject,
  type Credentials,
  type DiscordAllowlistRequestRecord,
  type ProjectAutomationRecord,
  type ProjectRecord,
} from "@/lib/api";

const PROJECT_AUTOMATION_KIND_STANDUP = "standup_voice_brief";
const PROJECT_AUTOMATION_KIND_RETRO = "retro_voice_brief";
const PROJECT_AUTOMATION_WEEKDAYS = [
  { value: 0, label: "Sun" },
  { value: 1, label: "Mon" },
  { value: 2, label: "Tue" },
  { value: 3, label: "Wed" },
  { value: 4, label: "Thu" },
  { value: 5, label: "Fri" },
  { value: 6, label: "Sat" },
];

type AutomationExecutionRow = ProjectAutomationExecutionRecord & {
  automation_kind: string;
  automation_label: string;
};

function getBrowserTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

function getAutomationLabel(kind: string): string {
  if (kind === PROJECT_AUTOMATION_KIND_STANDUP) {
    return "Standup voice brief";
  }
  if (kind === PROJECT_AUTOMATION_KIND_RETRO) {
    return "Retro voice brief";
  }
  return kind;
}

function createAutomationDraft(kind: string, overrides: Partial<ProjectAutomationRecord> = {}): ProjectAutomationRecord {
  const timestamp = new Date().toISOString();
  const timezone = getBrowserTimezone();
  const common = {
    automation_id: `draft-${kind}`,
    project_id: "",
    tenant_id: "",
    kind,
    enabled: false,
    timezone,
    days_of_week: kind === PROJECT_AUTOMATION_KIND_STANDUP ? [1, 2, 3, 4, 5] : [5],
    local_time: kind === PROJECT_AUTOMATION_KIND_STANDUP ? "09:30" : "16:00",
    delivery_text_channel_id: null,
    voice_id: null,
    fallback_lookback_hours: kind === PROJECT_AUTOMATION_KIND_STANDUP ? 24 : 168,
    last_successful_window_end_at: null,
    next_run_at: timestamp,
    executions: [],
    created_at: timestamp,
    updated_at: timestamp,
  } satisfies ProjectAutomationRecord;
  return { ...common, ...overrides, automation_id: overrides.automation_id ?? common.automation_id };
}

function defaultAutomationDrafts(): ProjectAutomationRecord[] {
  return [
    createAutomationDraft(PROJECT_AUTOMATION_KIND_STANDUP),
    createAutomationDraft(PROJECT_AUTOMATION_KIND_RETRO),
  ];
}

function automationKindMap(automations: ProjectAutomationRecord[]): Map<string, string> {
  return new Map(automations.map((automation) => [automation.automation_id, automation.kind]));
}

function flattenAutomationExecutions(automations: ProjectAutomationRecord[]): AutomationExecutionRow[] {
  const kindByAutomationId = automationKindMap(automations);
  return automations
    .flatMap((automation) =>
      (automation.executions ?? []).map((execution) => ({
        ...execution,
        automation_kind: automation.kind,
        automation_label: getAutomationLabel(automation.kind),
      })),
    )
    .sort((left, right) => {
      const leftTime = new Date(left.scheduled_for).getTime();
      const rightTime = new Date(right.scheduled_for).getTime();
      if (rightTime !== leftTime) {
        return rightTime - leftTime;
      }
      return (kindByAutomationId.get(right.automation_id) ?? right.automation_kind).localeCompare(
        kindByAutomationId.get(left.automation_id) ?? left.automation_kind,
      );
    })
    .slice(0, 20);
}

function isDraftAutomation(automation: ProjectAutomationRecord): boolean {
  return automation.automation_id.startsWith("draft-");
}

// ─── Project voice automations (dedicated project tab) ─────────────────────────

type ProjectAutomationsContentProps = {
  tenantId: string;
  projectId: string;
  credentials: Credentials | null;
};

export function ProjectAutomationsContent({
  tenantId,
  projectId,
  credentials,
}: ProjectAutomationsContentProps) {
  const [automationDefinitions, setAutomationDefinitions] = useState<ProjectAutomationRecord[]>([]);
  const [automationBusy, setAutomationBusy] = useState(false);
  const [automationStatusLine, setAutomationStatusLine] = useState("");
  const automationExecutions = flattenAutomationExecutions(automationDefinitions);

  useEffect(() => {
    if (!credentials) return;
    void (async () => {
      try {
        const automations = await getProjectAutomations(credentials, tenantId, projectId);
        const loadedAutomations = automations.automations ?? [];
        setAutomationDefinitions(loadedAutomations.length > 0 ? loadedAutomations : defaultAutomationDrafts());
        setAutomationStatusLine("");
      } catch (error) {
        setAutomationStatusLine(`Failed to load automations: ${(error as Error).message}`);
      }
    })();
  }, [credentials, tenantId, projectId]);

  function updateAutomationDaysOfWeek(index: number, dayValue: number, checked: boolean): void {
    setAutomationDefinitions((current) =>
      current.map((automation, currentIndex) => {
        if (currentIndex !== index) {
          return automation;
        }
        const nextDays = checked
          ? Array.from(new Set([...automation.days_of_week, dayValue])).sort((left, right) => left - right)
          : automation.days_of_week.filter((day) => day !== dayValue);
        return { ...automation, days_of_week: nextDays };
      })
    );
  }

  function updateAutomationField(index: number, updates: Partial<ProjectAutomationRecord>): void {
    setAutomationDefinitions((current) =>
      current.map((automation, currentIndex) => (currentIndex === index ? { ...automation, ...updates } : automation))
    );
  }

  async function saveAutomations() {
    if (!credentials) return;
    setAutomationBusy(true);
    try {
      const updated = await updateProjectAutomations(credentials, tenantId, projectId, {
        automations: automationDefinitions.map((automation) => ({
          kind: automation.kind,
          enabled: automation.enabled,
          timezone: automation.timezone,
          days_of_week: automation.days_of_week,
          local_time: automation.local_time,
          delivery_text_channel_id: automation.delivery_text_channel_id?.trim() || null,
          voice_id: automation.voice_id,
          fallback_lookback_hours: automation.fallback_lookback_hours,
        })),
      });
      const savedAutomations = updated.automations ?? [];
      setAutomationDefinitions(savedAutomations.length > 0 ? savedAutomations : defaultAutomationDrafts());
      setAutomationStatusLine(`Saved ${updated.automations?.length ?? 0} automation configuration${(updated.automations?.length ?? 0) === 1 ? "" : "s"}.`);
    } catch (error) {
      setAutomationStatusLine(`Save failed: ${(error as Error).message}`);
    } finally {
      setAutomationBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      {automationStatusLine ? (
        <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{automationStatusLine}</p>
      ) : null}

      <Card id="project-automations">
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Project Automations</CardTitle>
          <p className="text-sm text-muted-foreground">
            Configure scheduled standup/retro voice brief automations for this project. Deliveries use your Discord text
            channel IDs.
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-3">
            {automationDefinitions.map((automation, index) => {
              const automationId = automation.automation_id || `automation-${index}`;
              const isDraft = isDraftAutomation(automation);
              return (
                <div
                  key={automationId}
                  data-testid={`project-automation-${automation.kind}`}
                  className="rounded-md border p-4 space-y-4"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="space-y-1">
                      <p className="text-sm font-medium">{getAutomationLabel(automation.kind)}</p>
                      <p className="text-xs text-muted-foreground">{isDraft ? "Draft automation" : automation.automation_id}</p>
                    </div>
                    <label className="flex items-center gap-2 text-xs">
                      <input
                        type="checkbox"
                        className="h-4 w-4 rounded border-input"
                        checked={automation.enabled}
                        onChange={(event) => updateAutomationField(index, { enabled: event.target.checked })}
                      />
                      Enabled
                    </label>
                  </div>
                  <div className="grid gap-4 md:grid-cols-2">
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground" htmlFor={`${automationId}-timezone`}>
                        Timezone
                      </label>
                      <Input
                        id={`${automationId}-timezone`}
                        value={automation.timezone}
                        onChange={(event) => updateAutomationField(index, { timezone: event.target.value })}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground" htmlFor={`${automationId}-local-time`}>
                        Local time
                      </label>
                      <Input
                        id={`${automationId}-local-time`}
                        value={automation.local_time}
                        onChange={(event) => updateAutomationField(index, { local_time: event.target.value })}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <label
                        className="text-xs font-medium uppercase tracking-wide text-muted-foreground"
                        htmlFor={`${automationId}-channel`}
                      >
                        Delivery text channel ID (optional)
                      </label>
                      <Input
                        id={`${automationId}-channel`}
                        value={automation.delivery_text_channel_id ?? ""}
                        onChange={(event) =>
                          updateAutomationField(index, {
                            delivery_text_channel_id: event.target.value.trim() || null,
                          })
                        }
                        placeholder="Leave blank until ready; required when a run delivers to Discord"
                      />
                    </div>
                    <div className="space-y-1.5">
                      <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground" htmlFor={`${automationId}-voice`}>
                        Voice ID
                      </label>
                      <Input
                        id={`${automationId}-voice`}
                        value={automation.voice_id ?? ""}
                        onChange={(event) => updateAutomationField(index, { voice_id: event.target.value || null })}
                        placeholder="alloy"
                      />
                    </div>
                    <div className="space-y-1.5">
                      <label
                        className="text-xs font-medium uppercase tracking-wide text-muted-foreground"
                        htmlFor={`${automationId}-lookback`}
                      >
                        Fallback lookback hours
                      </label>
                      <Input
                        id={`${automationId}-lookback`}
                        type="number"
                        min={1}
                        value={automation.fallback_lookback_hours}
                        onChange={(event) =>
                          updateAutomationField(index, { fallback_lookback_hours: Number(event.target.value) || 0 })
                        }
                      />
                    </div>
                  </div>
                  <div className="space-y-2">
                    <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Days of week</p>
                    <div className="flex flex-wrap gap-2">
                      {PROJECT_AUTOMATION_WEEKDAYS.map((day) => {
                        const checked = automation.days_of_week.includes(day.value);
                        return (
                          <label
                            key={`${automationId}-${day.value}`}
                            className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm"
                          >
                            <input
                              type="checkbox"
                              className="h-4 w-4 rounded border-input"
                              checked={checked}
                              onChange={(event) => updateAutomationDaysOfWeek(index, day.value, event.target.checked)}
                            />
                            <span>{day.label}</span>
                          </label>
                        );
                      })}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
          <div className="flex items-center justify-between gap-3 border-t pt-4">
            <p className="text-xs text-muted-foreground">
              Automation executions are retained separately so admins can review recent project automation activity.
            </p>
            <Button type="button" size="sm" onClick={() => void saveAutomations()} disabled={automationBusy || !credentials}>
              {automationBusy ? "Saving…" : "Save automations"}
            </Button>
          </div>
          <div className="space-y-2">
            <p className="text-sm font-medium">Recent executions</p>
            {automationExecutions.length === 0 ? (
              <p className="text-xs text-muted-foreground">No automation executions recorded for this project yet.</p>
            ) : (
              <ul className="space-y-2">
                {automationExecutions.map((execution) => (
                  <li
                    key={execution.execution_id}
                    data-testid={`project-automation-execution-${execution.execution_id}`}
                    className="rounded-md border bg-muted/20 p-3 text-sm"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <span className="font-medium">{execution.automation_label}</span>
                        <p className="text-xs text-muted-foreground">{execution.automation_id}</p>
                      </div>
                      <span className="text-xs text-muted-foreground">{execution.status}</span>
                    </div>
                    <div className="mt-2 space-y-1 text-xs text-muted-foreground">
                      <p>Scheduled for: {execution.scheduled_for}</p>
                      <p>Completed at: {execution.completed_at ?? "not completed yet"}</p>
                      <p>Discord message ID: {execution.discord_message_id ?? "not recorded"}</p>
                      {execution.last_error ? <p className="text-destructive">Last error: {execution.last_error}</p> : null}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// ─── Discord notifications & allowlist (Notifications project tab) ─────────────

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
        const [payload, requests] = await Promise.all([
          getProject(credentials, tenantId, projectId),
          listDiscordAllowlistRequests(credentials, tenantId, projectId),
        ]);
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
          <Link href={`/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(params.projectId)}`}>
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
