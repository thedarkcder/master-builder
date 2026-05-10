"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft, CheckCircle2, Clock, MessageSquare, User } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { DiscordSection } from "@/components/tenant-form-sections";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast-provider";

import { Input } from "@/components/ui/input";
import {
  approveDiscordAllowlistRequest,
  getProjectAutomations,
  getProject,
  listDiscordAllowlistRequests,
  runProjectAutomationNow,
  type ProjectAutomationExecutionRecord,
  updateProjectAutomations,
  updateProjectDiscord,
  type Credentials,
  type DiscordAllowlistRequestRecord,
  type ProjectAutomationRecord,
  type ProjectRecord,
} from "@/lib/api";
import { formatTimestamp } from "@/lib/datetime";

const PROJECT_AUTOMATION_KIND_STANDUP = "standup_voice_brief";
const PROJECT_AUTOMATION_KIND_RETRO = "retro_voice_brief";
const PROJECT_AUTOMATION_WEEKDAYS = [
  { value: 0, label: "Mon" },
  { value: 1, label: "Tue" },
  { value: 2, label: "Wed" },
  { value: 3, label: "Thu" },
  { value: 4, label: "Fri" },
  { value: 5, label: "Sat" },
  { value: 6, label: "Sun" },
];

type AutomationExecutionRow = ProjectAutomationExecutionRecord & {
  automation_kind: string;
  automation_label: string;
};

function getBrowserTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

function listTimezoneOptions(): string[] {
  const intlWithSupportedValues = Intl as unknown as {
    supportedValuesOf?: (key: string) => string[];
  };
  const values = typeof intlWithSupportedValues.supportedValuesOf === "function"
    ? intlWithSupportedValues.supportedValuesOf("timeZone")
    : [];
  if (Array.isArray(values) && values.length > 0) {
    return values;
  }
  return ["UTC", "America/New_York", "America/Los_Angeles", "Europe/London", "Europe/Berlin", "Asia/Tokyo"];
}

const TIMEZONE_OPTIONS = listTimezoneOptions();

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
    days_of_week: kind === PROJECT_AUTOMATION_KIND_STANDUP ? [0, 1, 2, 3, 4] : [4],
    local_time: kind === PROJECT_AUTOMATION_KIND_STANDUP ? "09:30" : "16:00",
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
    });
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
  const { principal } = useAuth();
  const { showToast } = useToast();
  const isPlatformSuperAdmin = principal?.principal_type === "platform_super_admin";
  const [automationDefinitions, setAutomationDefinitions] = useState<ProjectAutomationRecord[]>([]);
  const [automationBusy, setAutomationBusy] = useState(false);
  const [automationStatusLine, setAutomationStatusLine] = useState("");
  const [executionsPage, setExecutionsPage] = useState(1);
  const EXECUTIONS_PAGE_SIZE = 5;
  const automationExecutions = flattenAutomationExecutions(automationDefinitions);
  const executionsTotalPages = Math.max(1, Math.ceil(automationExecutions.length / EXECUTIONS_PAGE_SIZE));
  const pagedExecutions = automationExecutions.slice(
    (executionsPage - 1) * EXECUTIONS_PAGE_SIZE,
    executionsPage * EXECUTIONS_PAGE_SIZE,
  );

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
          fallback_lookback_hours: automation.fallback_lookback_hours,
        })),
      });
      const savedAutomations = updated.automations ?? [];
      setAutomationDefinitions(savedAutomations.length > 0 ? savedAutomations : defaultAutomationDrafts());
      showToast({
        title: "Automations saved",
        description: `${updated.automations?.length ?? 0} configuration${(updated.automations?.length ?? 0) === 1 ? "" : "s"}.`,
        tone: "success",
      });
    } catch (error) {
      showToast({ title: "Automation save failed", description: (error as Error).message, tone: "error" });
    } finally {
      setAutomationBusy(false);
    }
  }

  async function runAutomationNow(kind: string) {
    if (!credentials || !isPlatformSuperAdmin) return;
    setAutomationBusy(true);
    try {
      const updated = await runProjectAutomationNow(credentials, tenantId, projectId, kind);
      const savedAutomations = updated.automations ?? [];
      setAutomationDefinitions(savedAutomations.length > 0 ? savedAutomations : defaultAutomationDrafts());
      showToast({ title: "Automation queued", description: getAutomationLabel(kind), tone: "success" });
    } catch (error) {
      showToast({ title: "Automation run failed", description: (error as Error).message, tone: "error" });
    } finally {
      setAutomationBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      {automationStatusLine ? (
        <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{automationStatusLine}</p>
      ) : null}

      <div id="project-automations" className="overflow-hidden rounded-2xl border bg-background">
        <div className="flex flex-wrap items-center justify-between gap-3 px-6 pt-6 pb-3">
          <div>
            <h2 className="text-base font-semibold">Project Automations</h2>
          </div>
          <Button type="button" size="sm" onClick={() => void saveAutomations()} disabled={automationBusy || !credentials}>
            {automationBusy ? "Saving…" : "Save automations"}
          </Button>
        </div>
        <div className="divide-y">
          {automationDefinitions.map((automation, index) => {
            const automationId = automation.automation_id || `automation-${index}`;
            const isDraft = isDraftAutomation(automation);
            return (
              <section
                key={automationId}
                data-testid={`project-automation-${automation.kind}`}
                className="space-y-4 px-6 py-5"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="space-y-1">
                    <p className="text-sm font-medium">{getAutomationLabel(automation.kind)}</p>
                    <p className="text-xs text-muted-foreground">{isDraft ? "Draft automation" : automationId}</p>
                  </div>
                  <div className="flex items-center gap-3">
                    {isPlatformSuperAdmin ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="secondary"
                        onClick={() => void runAutomationNow(automation.kind)}
                        disabled={automationBusy || isDraft || !automation.enabled}
                      >
                        Run now
                      </Button>
                    ) : null}
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
                </div>
                <div className="grid gap-4 md:grid-cols-3">
                  <div className="space-y-1.5">
                    <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground" htmlFor={`${automationId}-timezone`}>
                      Timezone
                    </label>
                    <select
                      id={`${automationId}-timezone`}
                      value={automation.timezone}
                      onChange={(event) => updateAutomationField(index, { timezone: event.target.value })}
                      className="h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                    >
                      {TIMEZONE_OPTIONS.map((tz) => (
                        <option key={tz} value={tz}>
                          {tz}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground" htmlFor={`${automationId}-local-time`}>
                      Local time
                    </label>
                    <Input
                      id={`${automationId}-local-time`}
                      type="time"
                      value={automation.local_time}
                      onChange={(event) => updateAutomationField(index, { local_time: event.target.value })}
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
              </section>
            );
          })}
        </div>
      </div>

      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="flex items-center justify-between gap-2 px-6 pt-6 pb-3">
          <div>
            <h2 className="text-base font-semibold">Recent executions</h2>
          </div>
          {automationExecutions.length > 0 ? (
            <span className="text-xs text-muted-foreground">{automationExecutions.length} total</span>
          ) : null}
        </div>
        {automationExecutions.length === 0 ? (
          <div className="px-6 pb-6">
            <p className="text-sm text-muted-foreground">No automation executions recorded for this project yet.</p>
          </div>
        ) : (
          <>
            <div className="divide-y">
              {pagedExecutions.map((execution) => (
                <div
                  key={execution.execution_id}
                  data-testid={`project-automation-execution-${execution.execution_id}`}
                  className="px-6 py-4 text-sm"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <span className="font-medium">{execution.automation_label}</span>
                      <p className="text-xs text-muted-foreground">{execution.automation_id}</p>
                    </div>
                    <span className="text-xs text-muted-foreground">{execution.status}</span>
                  </div>
                  <div className="mt-2 grid gap-x-6 gap-y-1 text-xs text-muted-foreground sm:grid-cols-3">
                    <p>Scheduled: {execution.scheduled_for}</p>
                    <p>Completed: {execution.completed_at ?? "not yet"}</p>
                    <p>Discord msg: {execution.discord_message_id ?? "—"}</p>
                  </div>
                  {execution.last_error ? (
                    <p className="mt-1 text-xs text-destructive">Error: {execution.last_error}</p>
                  ) : null}
                </div>
              ))}
            </div>
            {executionsTotalPages > 1 ? (
              <div className="flex items-center justify-end gap-2 border-t px-6 py-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setExecutionsPage((p) => Math.max(1, p - 1))}
                  disabled={executionsPage <= 1}
                >
                  ← Prev
                </Button>
                <span className="text-xs text-muted-foreground">
                  Page {executionsPage} of {executionsTotalPages}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setExecutionsPage((p) => Math.min(executionsTotalPages, p + 1))}
                  disabled={executionsPage >= executionsTotalPages}
                >
                  Next →
                </Button>
              </div>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}

// ─── Discord notifications & allowlist (Notifications project tab) ─────────────

type ProjectNotificationsContentProps = {
  tenantId: string;
  projectId: string;
  credentials: Credentials | null;
  onAllowlistRequestsChange?: (requests: DiscordAllowlistRequestRecord[]) => void;
};

export function ProjectNotificationsContent({
  tenantId,
  projectId,
  credentials,
  onAllowlistRequestsChange,
}: ProjectNotificationsContentProps) {
  const { showToast } = useToast();
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
        onAllowlistRequestsChange?.(requests);
        setStatusLine("");
      } catch (error) {
        setStatusLine(`Failed to load project: ${(error as Error).message}`);
      } finally {
        setBusy(false);
      }
    })();
  }, [credentials, onAllowlistRequestsChange, tenantId, projectId]);

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
      const updated = await updateProjectDiscord(credentials, tenantId, projectId, {
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
        showToast({
          title: "Discord settings saved",
          description: `Live voice room ${savedVoiceChannelId} is linked to ${savedLinkedTextChannelId}.`,
          tone: "success",
        });
      } else {
        showToast({ title: "Discord settings saved", description: "Live voice is disabled for this project.", tone: "success" });
      }
    } catch (error) {
      showToast({ title: "Discord settings save failed", description: (error as Error).message, tone: "error" });
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
      onAllowlistRequestsChange?.(requests);
      showToast({ title: "Allowlist request approved", description: result.details, tone: "success" });
    } catch (error) {
      showToast({ title: "Allowlist approval failed", description: (error as Error).message, tone: "error" });
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
      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="p-6 pb-3">
          <h2 className="text-base font-semibold">Notification Settings</h2>
        </div>
        <div className="space-y-4 p-6 pt-0">
          <DiscordSection
            title="Project Discord"
            description="Enable notifications for this project and choose which events should be posted."
            discordEnabled={discordEnabled}
            notifyEvents={notifyEvents}
            onDiscordEnabledChange={setDiscordEnabled}
            onToggleDiscordNotifyEvent={toggleNotifyEvent}
          />
          {discordEnabled ? (
            <div className="space-y-4 border-t pt-4">
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
        </div>
      </div>

      {/* Access Requests */}
      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="p-6 pb-3">
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-base font-semibold">Access Requests</h2>
            {allowlistRequests.length > 0 ? (
              <span className="flex h-5 w-5 items-center justify-center rounded-full bg-warning text-[10px] font-bold text-white">
                {allowlistRequests.length}
              </span>
            ) : null}
          </div>
        </div>
        {allowlistRequests.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 px-6 pb-6 pt-3 text-center">
            <CheckCircle2 className="h-6 w-6 text-success" />
            <p className="text-sm font-medium">No pending requests</p>
            <p className="text-xs text-muted-foreground">All access requests have been handled.</p>
          </div>
        ) : (
          <div className="divide-y">
            {allowlistRequests.map((request) => (
              <div
                key={request.user_id}
                className="flex flex-wrap items-start justify-between gap-3 px-6 py-4"
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
                    {formatTimestamp(request.requested_at)}
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
              </div>
            ))}
          </div>
        )}
      </div>
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
