"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";

import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import {
  createProjectKnowledgeSource,
  deleteProjectKnowledgeSource,
  getKnowledgeJiraSyncRuntimeStatus,
  listProjectKnowledgeSources,
  syncProjectKnowledgeSource,
  updateProjectKnowledgeSource,
  type Credentials,
  type KnowledgeJiraSyncProjectStatusRecord,
  type ProjectKnowledgeSourceCreatePayload,
  type ProjectKnowledgeSourceRecord,
} from "@/lib/api";

type ProjectKnowledgeSourcesSectionProps = {
  credentials: Credentials | null;
  tenantId: string;
  projectId: string;
};

type ConnectorType = "jira" | "google_drive" | "discord";

function formatTimestamp(value: string | null): string {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function splitLines(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function ProjectKnowledgeSourcesSection({
  credentials,
  tenantId,
  projectId,
}: ProjectKnowledgeSourcesSectionProps) {
  const [sources, setSources] = useState<ProjectKnowledgeSourceRecord[]>([]);
  const [runtimeStatus, setRuntimeStatus] = useState<KnowledgeJiraSyncProjectStatusRecord | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [syncingSourceId, setSyncingSourceId] = useState<string | null>(null);
  const [mutatingSourceId, setMutatingSourceId] = useState<string | null>(null);
  const [statusLine, setStatusLine] = useState("");

  const [connectorType, setConnectorType] = useState<ConnectorType>("jira");
  const [displayName, setDisplayName] = useState("");
  const [jiraProjectKey, setJiraProjectKey] = useState("");
  const [googleDriveTargets, setGoogleDriveTargets] = useState("");
  const [discordChannelIds, setDiscordChannelIds] = useState("");
  const [discordThreadIds, setDiscordThreadIds] = useState("");
  const [syncMode, setSyncMode] = useState<"manual" | "scheduled">("scheduled");

  const ensureCredentials = useCallback((): Credentials | null => {
    if (!credentials) {
      setStatusLine("Admin session is not ready yet. Wait a moment and try again.");
      return null;
    }
    return credentials;
  }, [credentials]);

  const loadSources = useCallback(async () => {
    if (!credentials) {
      return;
    }
    setLoading(true);
    try {
      const [sourcePage, runtime] = await Promise.all([
        listProjectKnowledgeSources(credentials, tenantId, projectId),
        getKnowledgeJiraSyncRuntimeStatus(credentials),
      ]);
      setSources(sourcePage.items);
      setRuntimeStatus(
        runtime.projects.find(
          (projectStatus) => projectStatus.tenant_id === tenantId && projectStatus.project_id === projectId,
        ) ?? null,
      );
    } catch (error) {
      setStatusLine(`Unable to load project sources: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [credentials, tenantId, projectId]);

  useEffect(() => {
    if (!credentials) {
      return;
    }
    void loadSources();
  }, [credentials, loadSources]);

  useEffect(() => {
    setSyncMode(connectorType === "jira" ? "scheduled" : "manual");
  }, [connectorType]);

  const sourceCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const source of sources) {
      counts.set(source.connector_type, (counts.get(source.connector_type) ?? 0) + 1);
    }
    return Array.from(counts.entries())
      .sort((left, right) => left[0].localeCompare(right[0]))
      .map(([type, count]) => `${type}: ${count}`)
      .join(" · ");
  }, [sources]);

  function resetForm() {
    setDisplayName("");
    setJiraProjectKey("");
    setGoogleDriveTargets("");
    setDiscordChannelIds("");
    setDiscordThreadIds("");
    setConnectorType("jira");
    setSyncMode("scheduled");
  }

  function buildPayload(): ProjectKnowledgeSourceCreatePayload {
    if (connectorType === "jira") {
      return {
        connector_type: "jira",
        display_name: displayName || undefined,
        sync_mode: syncMode,
        config_json: jiraProjectKey.trim() ? { project_key: jiraProjectKey.trim() } : {},
      };
    }
    if (connectorType === "google_drive") {
      return {
        connector_type: "google_drive",
        display_name: displayName || undefined,
        sync_mode: "manual",
        config_json: { targets: splitLines(googleDriveTargets) },
      };
    }
    return {
      connector_type: "discord",
      display_name: displayName || undefined,
      sync_mode: "manual",
      config_json: {
        channel_ids: splitLines(discordChannelIds),
        thread_ids: splitLines(discordThreadIds),
      },
    };
  }

  async function addSource() {
    const activeCredentials = ensureCredentials();
    if (!activeCredentials) {
      return;
    }
    setSaving(true);
    try {
      const source = await createProjectKnowledgeSource(activeCredentials, tenantId, projectId, buildPayload());
      resetForm();
      await loadSources();
      setStatusLine(`Added source "${source.display_name}".`);
    } catch (error) {
      setStatusLine(`Unable to add source: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function toggleSource(source: ProjectKnowledgeSourceRecord) {
    const activeCredentials = ensureCredentials();
    if (!activeCredentials) {
      return;
    }
    setMutatingSourceId(source.source_id);
    try {
      const nextStatus = source.status === "active" ? "disabled" : "active";
      const updated = await updateProjectKnowledgeSource(activeCredentials, tenantId, projectId, source.source_id, {
        status: nextStatus,
      });
      await loadSources();
      setStatusLine(`Source "${updated.display_name}" is now ${updated.status}.`);
    } catch (error) {
      setStatusLine(`Unable to update source: ${(error as Error).message}`);
    } finally {
      setMutatingSourceId(null);
    }
  }

  async function removeSource(source: ProjectKnowledgeSourceRecord) {
    const activeCredentials = ensureCredentials();
    if (!activeCredentials) {
      return;
    }
    setMutatingSourceId(source.source_id);
    try {
      await deleteProjectKnowledgeSource(activeCredentials, tenantId, projectId, source.source_id);
      await loadSources();
      setStatusLine(`Removed source "${source.display_name}".`);
    } catch (error) {
      setStatusLine(`Unable to remove source: ${(error as Error).message}`);
    } finally {
      setMutatingSourceId(null);
    }
  }

  async function runSync(source: ProjectKnowledgeSourceRecord) {
    const activeCredentials = ensureCredentials();
    if (!activeCredentials) {
      return;
    }
    setSyncingSourceId(source.source_id);
    try {
      const result = await syncProjectKnowledgeSource(activeCredentials, tenantId, projectId, source.source_id);
      await loadSources();
      setStatusLine(
        `Synced "${source.display_name}": created ${result.created_assets}, updated ${result.updated_assets}, unchanged ${result.unchanged_assets}, deleted ${result.deleted_assets}, failed ${result.failed_assets}.`,
      );
    } catch (error) {
      setStatusLine(`Unable to sync source: ${(error as Error).message}`);
    } finally {
      setSyncingSourceId(null);
    }
  }

  return (
    <div className="space-y-6">
      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="p-6 pb-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold">Managed Sources</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                Configure the upstream knowledge connectors for this project. The semantic layer stays source-agnostic.
              </p>
            </div>
            <Button variant="outline" size="sm" onClick={() => void loadSources()} disabled={!credentials || loading}>
              {loading ? "Refreshing..." : "Refresh"}
            </Button>
          </div>
        </div>
        <div className="space-y-3 p-6 pt-0">
          <div className="rounded-md border p-3 text-sm text-muted-foreground">
            <p>
              <span className="font-medium text-foreground">Configured connectors:</span>{" "}
              {sourceCounts || "No managed sources configured yet."}
            </p>
            {runtimeStatus ? (
              <p className="mt-1">
                <span className="font-medium text-foreground">Jira runtime state:</span>{" "}
                {runtimeStatus.state}
                {runtimeStatus.failure_category ? ` (${runtimeStatus.failure_category})` : ""}
              </p>
            ) : null}
          </div>

          {sources.length === 0 ? (
            <p className="rounded-md border p-4 text-sm text-muted-foreground">
              {loading ? "Loading sources..." : "No managed sources configured yet."}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Source</TableHead>
                  <TableHead>Connector</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Sync</TableHead>
                  <TableHead>Last sync</TableHead>
                  <TableHead>Notes</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sources.map((source) => (
                  <TableRow key={source.source_id}>
                    <TableCell>
                      <div className="space-y-1">
                        <p className="font-medium">{source.display_name}</p>
                        <p className="text-xs text-muted-foreground">{source.config_summary}</p>
                      </div>
                    </TableCell>
                    <TableCell>{source.connector_type}</TableCell>
                    <TableCell>{source.status}</TableCell>
                    <TableCell>{source.sync_mode}</TableCell>
                    <TableCell>{formatTimestamp(source.last_synced_at)}</TableCell>
                    <TableCell className="max-w-xs">
                      <p className="text-xs text-muted-foreground">
                        {source.last_error
                          ? source.last_error
                          : source.connector_type === "jira" && runtimeStatus?.last_error
                            ? runtimeStatus.last_error
                            : "No recent errors"}
                      </p>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex flex-wrap justify-end gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => void runSync(source)}
                          disabled={!credentials || !source.supports_sync_now || syncingSourceId === source.source_id}
                        >
                          {syncingSourceId === source.source_id ? "Syncing..." : "Sync now"}
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => void toggleSource(source)}
                          disabled={!credentials || mutatingSourceId === source.source_id}
                        >
                          {mutatingSourceId === source.source_id
                            ? "Saving..."
                            : source.status === "active"
                              ? "Disable"
                              : "Enable"}
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          className="text-red-700 hover:text-red-800"
                          onClick={() => void removeSource(source)}
                          disabled={!credentials || mutatingSourceId === source.source_id}
                        >
                          {mutatingSourceId === source.source_id ? "Removing..." : "Remove"}
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </div>
      </div>

      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="p-6 pb-3">
          <h2 className="text-base font-semibold">Add Source</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Add Jira, Google Drive, or Discord as a project knowledge source.
          </p>
        </div>
        <div className="space-y-4 p-6 pt-0">
          <div className="flex flex-wrap gap-2">
            {(["jira", "google_drive", "discord"] as const).map((value) => (
              <Button
                key={value}
                type="button"
                variant={connectorType === value ? "default" : "outline"}
                size="sm"
                onClick={() => setConnectorType(value)}
                disabled={saving}
              >
                {value === "jira" ? "Jira" : value === "google_drive" ? "Google Drive" : "Discord"}
              </Button>
            ))}
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <Input
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              placeholder="Display name (optional)"
            />
            {connectorType === "jira" ? (
              <div className="grid gap-3 md:grid-cols-2">
                <Input
                  value={jiraProjectKey}
                  onChange={(event) => setJiraProjectKey(event.target.value)}
                  placeholder="Jira project key"
                />
                <select
                  className="h-10 rounded-md border bg-background px-3 text-sm"
                  value={syncMode}
                  onChange={(event) => setSyncMode(event.target.value as "manual" | "scheduled")}
                >
                  <option value="scheduled">Scheduled sync</option>
                  <option value="manual">Manual sync</option>
                </select>
              </div>
            ) : null}
          </div>

          {connectorType === "google_drive" ? (
            <Textarea
              value={googleDriveTargets}
              onChange={(event) => setGoogleDriveTargets(event.target.value)}
              placeholder={"One Google Drive document, file, or folder ID/URL per line"}
              rows={5}
            />
          ) : null}

          {connectorType === "discord" ? (
            <div className="grid gap-3 md:grid-cols-2">
              <Textarea
                value={discordChannelIds}
                onChange={(event) => setDiscordChannelIds(event.target.value)}
                placeholder={"Discord channel IDs, one per line"}
                rows={5}
              />
              <Textarea
                value={discordThreadIds}
                onChange={(event) => setDiscordThreadIds(event.target.value)}
                placeholder={"Discord thread IDs, one per line"}
                rows={5}
              />
            </div>
          ) : null}

          <div className="flex justify-end">
            <Button onClick={() => void addSource()} disabled={!credentials || saving}>
              {saving ? "Adding..." : "Add source"}
            </Button>
          </div>
        </div>
      </div>

      {statusLine ? <p className="rounded-md border px-3 py-2 text-sm text-muted-foreground">{statusLine}</p> : null}
    </div>
  );
}
