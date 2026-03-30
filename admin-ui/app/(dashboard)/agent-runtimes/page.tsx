"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Bot, Plus, RefreshCw, RotateCcw, Save, Trash2 } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { CodexModelSelect } from "@/components/codex-model-select";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  createAgentRuntimeProfile,
  deleteAgentRuntimeProfile,
  getAgentRuntimeRouting,
  listAgentRuntimeProfiles,
  listAgentRuntimeTools,
  listCodexModels,
  resetAgentRuntimeProfile,
  resetAgentRuntimeRouting,
  updateAgentRuntimeProfile,
  updateAgentRuntimeRouting,
  type AgentExecutionProfileCreatePayload,
  type AgentExecutionProfileRecord,
  type AgentExecutionProfileWritePayload,
  type AgentExecutionProfilesRecord,
  type AgentRuntimeToolRecord,
  type AgentRuntimeToolsRecord,
  type AgentRuntimeRoutingRecord,
  type CodexModelCatalogRecord,
} from "@/lib/api";
import { canAccessPlatformAdmin, getDefaultAuthenticatedRoute } from "@/lib/auth-routing";

type RuntimeTab = "routing" | "profiles" | "tools";

type ProfileDraft = {
  profile_name: string;
  runtime_kind: string;
  cli_command: string;
  model: string | null;
  reasoning_effort: "low" | "medium" | "high" | null;
  tool_bridge_allowed: boolean;
  fallback_profile: string | null;
  base_url: string | null;
  api_key_secret_ref: string | null;
};

const TAB_OPTIONS: { id: RuntimeTab; label: string }[] = [
  { id: "routing", label: "Routing" },
  { id: "profiles", label: "Profiles" },
  { id: "tools", label: "Tools" },
];

const RUNTIME_OPTIONS = [
  { id: "codex_cli", label: "Codex CLI" },
  { id: "chat_cli", label: "Chat CLI" },
  { id: "claude_cli", label: "Claude CLI" },
  { id: "openai", label: "OpenAI API" },
  { id: "claude", label: "Claude API" },
  { id: "llama_cpp", label: "llama.cpp" },
  { id: "lm_studio", label: "LM Studio" },
];

function defaultCliCommandForRuntime(runtimeKind: string): string {
  if (runtimeKind === "claude_cli") return "claude";
  if (runtimeKind === "chat_cli") return "chat";
  if (runtimeKind === "codex_cli") return "codex";
  return "";
}

function defaultBaseUrlForRuntime(runtimeKind: string): string | null {
  if (runtimeKind === "openai") return "https://api.openai.com/v1";
  if (runtimeKind === "claude") return "https://api.anthropic.com";
  if (runtimeKind === "llama_cpp") return "http://localhost:8080/v1";
  if (runtimeKind === "lm_studio") return "http://localhost:1234/v1";
  return null;
}

function applyRuntimeTransportDefaults(draft: ProfileDraft, runtimeKind: string): ProfileDraft {
  const nextCliCommand = currentOrDefaultCliCommand(draft.cli_command, runtimeKind);
  const nextBaseUrl = currentOrDefaultBaseUrl(draft.base_url, runtimeKind);
  const usesCli = runtimeKind === "codex_cli" || runtimeKind === "chat_cli" || runtimeKind === "claude_cli";
  const usesBaseUrl = runtimeKind === "openai" || runtimeKind === "claude" || runtimeKind === "llama_cpp" || runtimeKind === "lm_studio";
  const allowsApiKey = runtimeKind === "openai" || runtimeKind === "claude" || runtimeKind === "llama_cpp" || runtimeKind === "lm_studio";
  return {
    ...draft,
    runtime_kind: runtimeKind,
    cli_command: usesCli ? nextCliCommand : "",
    base_url: usesBaseUrl ? nextBaseUrl : null,
    api_key_secret_ref: allowsApiKey ? draft.api_key_secret_ref : null,
  };
}

function currentOrDefaultCliCommand(currentValue: string | null | undefined, runtimeKind: string): string {
  const normalizedCurrent = String(currentValue || "").trim();
  const knownDefaults = new Set(["codex", "chat", "claude"]);
  if (!normalizedCurrent || knownDefaults.has(normalizedCurrent)) {
    return defaultCliCommandForRuntime(runtimeKind);
  }
  return normalizedCurrent;
}

function currentOrDefaultBaseUrl(currentValue: string | null | undefined, runtimeKind: string): string | null {
  const normalizedCurrent = String(currentValue || "").trim();
  const knownDefaults = new Set([
    "https://api.openai.com/v1",
    "https://api.anthropic.com",
    "http://localhost:8080/v1",
    "http://localhost:1234/v1",
  ]);
  if (normalizedCurrent && !knownDefaults.has(normalizedCurrent)) return normalizedCurrent;
  return defaultBaseUrlForRuntime(runtimeKind);
}

function profileSummary(profile: AgentExecutionProfileRecord | undefined): string {
  if (!profile) return "No profile selected";
  const parts = [
    profile.runtime_kind,
    profile.cli_command || profile.base_url || "no transport configured",
    profile.model,
    profile.reasoning_effort ?? "no reasoning override",
    profile.tool_bridge_allowed ? "tools on" : "tools off",
  ];
  if (profile.fallback_profile) {
    parts.push(`fallback ${profile.fallback_profile}`);
  }
  return parts.join(" · ");
}

function buildDraft(profile?: AgentExecutionProfileRecord | null): ProfileDraft {
  const runtimeKind = profile?.runtime_kind ?? "codex_cli";
  const draft: ProfileDraft = {
    profile_name: profile?.profile_name ?? "",
    runtime_kind: runtimeKind,
    cli_command: profile?.cli_command ?? "",
    model: profile?.model ?? null,
    reasoning_effort: profile?.reasoning_effort ?? null,
    tool_bridge_allowed: profile?.tool_bridge_allowed ?? true,
    fallback_profile: profile?.fallback_profile ?? null,
    base_url: profile?.base_url ?? null,
    api_key_secret_ref: profile?.api_key_secret_ref ?? null,
  };
  return applyRuntimeTransportDefaults(draft, runtimeKind);
}

function draftToWritePayload(draft: ProfileDraft): AgentExecutionProfileWritePayload {
  return {
    runtime_kind: draft.runtime_kind,
    cli_command: draft.cli_command.trim(),
    model: String(draft.model || "").trim(),
    reasoning_effort: draft.reasoning_effort,
    tool_bridge_allowed: draft.tool_bridge_allowed,
    fallback_profile: draft.fallback_profile?.trim() ? draft.fallback_profile.trim() : null,
    base_url: draft.base_url?.trim() ? draft.base_url.trim() : null,
    api_key_secret_ref: draft.api_key_secret_ref?.trim() ? draft.api_key_secret_ref.trim() : null,
  };
}

export default function AgentRuntimesPage() {
  const { credentials, principal, principalReady, ready } = useAuth();
  const [activeTab, setActiveTab] = useState<RuntimeTab>("routing");
  const [routing, setRouting] = useState<AgentRuntimeRoutingRecord | null>(null);
  const [profilesResponse, setProfilesResponse] = useState<AgentExecutionProfilesRecord | null>(null);
  const [toolsResponse, setToolsResponse] = useState<AgentRuntimeToolsRecord | null>(null);
  const [roleRouting, setRoleRouting] = useState<Record<string, string>>({});
  const [nameRouting, setNameRouting] = useState<Record<string, string>>({});
  const [selectorRouting, setSelectorRouting] = useState<Record<string, string>>({});
  const [routingLoading, setRoutingLoading] = useState(false);
  const [profilesLoading, setProfilesLoading] = useState(false);
  const [toolsLoading, setToolsLoading] = useState(false);
  const [savingRouting, setSavingRouting] = useState(false);
  const [savingProfile, setSavingProfile] = useState(false);
  const [routingStatusLine, setRoutingStatusLine] = useState("");
  const [profilesStatusLine, setProfilesStatusLine] = useState("");
  const [toolsStatusLine, setToolsStatusLine] = useState("");
  const [editingProfileName, setEditingProfileName] = useState<string | null>(null);
  const [draft, setDraft] = useState<ProfileDraft>(() => buildDraft());
  const [modelCatalog, setModelCatalog] = useState<CodexModelCatalogRecord | null>(null);

  const refreshRouting = useCallback(async (): Promise<void> => {
    if (!credentials) return;
    setRoutingLoading(true);
    try {
      const response = await getAgentRuntimeRouting(credentials);
      setRouting(response);
      setRoleRouting(response.role_routing);
      setNameRouting(response.name_routing);
      setSelectorRouting(response.selector_routing);
      setRoutingStatusLine("Loaded platform agent runtime routing.");
    } catch (error) {
      setRoutingStatusLine(`Failed to load agent runtimes: ${(error as Error).message}`);
    } finally {
      setRoutingLoading(false);
    }
  }, [credentials]);

  const refreshProfiles = useCallback(async (): Promise<void> => {
    if (!credentials) return;
    setProfilesLoading(true);
    try {
      const response = await listAgentRuntimeProfiles(credentials);
      setProfilesResponse(response);
      setProfilesStatusLine("Loaded runtime profiles.");
    } catch (error) {
      setProfilesStatusLine(`Failed to load runtime profiles: ${(error as Error).message}`);
    } finally {
      setProfilesLoading(false);
    }
  }, [credentials]);

  const refreshTools = useCallback(async (): Promise<void> => {
    if (!credentials) return;
    setToolsLoading(true);
    try {
      const response = await listAgentRuntimeTools(credentials);
      setToolsResponse(response);
      setToolsStatusLine("Loaded implemented tools.");
    } catch (error) {
      setToolsStatusLine(`Failed to load implemented tools: ${(error as Error).message}`);
    } finally {
      setToolsLoading(false);
    }
  }, [credentials]);

  const refreshAll = useCallback(async (): Promise<void> => {
    await Promise.all([refreshRouting(), refreshProfiles(), refreshTools()]);
  }, [refreshProfiles, refreshRouting, refreshTools]);

  const profiles = profilesResponse?.profiles ?? routing?.available_profiles ?? {};
  const sortedProfileNames = useMemo(() => Object.keys(profiles).sort(), [profiles]);
  const tools = toolsResponse?.tools ?? [];
  const selectedProfile = editingProfileName ? profiles[editingProfileName] : null;
  const runtimeKind = draft.runtime_kind;
  const reasoningEfforts = modelCatalog?.reasoning_efforts ?? [];
  const currentReasoningSupported = reasoningEfforts.length > 0;
  const currentTransportIsCli = runtimeKind === "codex_cli" || runtimeKind === "chat_cli" || runtimeKind === "claude_cli";
  const currentTransportNeedsBaseUrl = runtimeKind === "llama_cpp" || runtimeKind === "lm_studio";
  const currentTransportNeedsApiKey = runtimeKind === "openai" || runtimeKind === "claude";
  const currentTransportAllowsApiKey = currentTransportNeedsApiKey || runtimeKind === "llama_cpp" || runtimeKind === "lm_studio";
  const missingRequiredTransportField =
    (currentTransportIsCli && !draft.cli_command.trim()) ||
    ((currentTransportNeedsBaseUrl || runtimeKind === "openai" || runtimeKind === "claude") && !String(draft.base_url || "").trim()) ||
    (currentTransportNeedsApiKey && !String(draft.api_key_secret_ref || "").trim());

  const loadModelCatalog = useCallback(
    async (nextRuntimeKind: string, profileName: string | null) => {
      if (!credentials) return;
      try {
        const catalog = await listCodexModels(credentials, {
          runtimeKind: nextRuntimeKind,
          profileName: profileName?.trim() ? profileName.trim() : null,
        });
        setModelCatalog(catalog);
      } catch (error) {
        setProfilesStatusLine(`Failed to load models: ${(error as Error).message}`);
      }
    },
    [credentials],
  );

  async function saveRouting(): Promise<void> {
    if (!credentials) return;
    setSavingRouting(true);
    try {
      const response = await updateAgentRuntimeRouting(credentials, {
        role_routing: Object.fromEntries(Object.entries(roleRouting).filter(([, value]) => value)),
        name_routing: Object.fromEntries(Object.entries(nameRouting).filter(([, value]) => value)),
        selector_routing: Object.fromEntries(Object.entries(selectorRouting).filter(([, value]) => value)),
      });
      setRouting(response);
      setRoleRouting(response.role_routing);
      setNameRouting(response.name_routing);
      setSelectorRouting(response.selector_routing);
      setRoutingStatusLine("Saved platform agent runtime routing.");
      await refreshProfiles();
    } catch (error) {
      setRoutingStatusLine(`Save failed: ${(error as Error).message}`);
    } finally {
      setSavingRouting(false);
    }
  }

  async function resetRouting(): Promise<void> {
    if (!credentials) return;
    if (!window.confirm("Reset all platform agent runtime overrides?")) return;
    setSavingRouting(true);
    try {
      const response = await resetAgentRuntimeRouting(credentials);
      setRouting(response);
      setRoleRouting(response.role_routing);
      setNameRouting(response.name_routing);
      setSelectorRouting(response.selector_routing);
      setRoutingStatusLine("Reset platform agent runtime routing to inherited defaults.");
      await refreshProfiles();
    } catch (error) {
      setRoutingStatusLine(`Reset failed: ${(error as Error).message}`);
    } finally {
      setSavingRouting(false);
    }
  }

  async function saveProfile(): Promise<void> {
    if (!credentials) return;
    if (missingRequiredTransportField) {
      setProfilesStatusLine("Save failed: complete the required runtime fields before saving.");
      return;
    }
    setSavingProfile(true);
    try {
      if (editingProfileName) {
        const updated = await updateAgentRuntimeProfile(credentials, editingProfileName, draftToWritePayload(draft));
        setProfilesStatusLine(`Saved profile ${updated.profile_name}.`);
      } else {
        const payload: AgentExecutionProfileCreatePayload = {
          profile_name: draft.profile_name.trim(),
          ...draftToWritePayload(draft),
        };
        const created = await createAgentRuntimeProfile(credentials, payload);
        setEditingProfileName(created.profile_name);
        setProfilesStatusLine(`Created profile ${created.profile_name}.`);
      }
      await refreshAll();
    } catch (error) {
      setProfilesStatusLine(`Save failed: ${(error as Error).message}`);
    } finally {
      setSavingProfile(false);
    }
  }

  async function resetProfile(): Promise<void> {
    if (!credentials || !editingProfileName) return;
    if (!window.confirm(`Reset ${editingProfileName} back to its built-in defaults?`)) return;
    setSavingProfile(true);
    try {
      const resetProfileRecord = await resetAgentRuntimeProfile(credentials, editingProfileName);
      setDraft(buildDraft(resetProfileRecord));
      setProfilesStatusLine(`Reset profile ${resetProfileRecord.profile_name}.`);
      await refreshAll();
    } catch (error) {
      setProfilesStatusLine(`Reset failed: ${(error as Error).message}`);
    } finally {
      setSavingProfile(false);
    }
  }

  async function deleteProfile(): Promise<void> {
    if (!credentials || !editingProfileName) return;
    if (!window.confirm(`Delete custom profile ${editingProfileName}?`)) return;
    setSavingProfile(true);
    try {
      await deleteAgentRuntimeProfile(credentials, editingProfileName);
      setEditingProfileName(null);
      setDraft(buildDraft());
      setProfilesStatusLine(`Deleted profile ${editingProfileName}.`);
      await refreshAll();
    } catch (error) {
      setProfilesStatusLine(`Delete failed: ${(error as Error).message}`);
    } finally {
      setSavingProfile(false);
    }
  }

  useEffect(() => {
    if (!ready || !principalReady || !principal) {
      return;
    }
    if (!canAccessPlatformAdmin(principal)) {
      window.location.replace(getDefaultAuthenticatedRoute(principal));
      return;
    }
    if (credentials) {
      void refreshAll();
    }
  }, [credentials, principal, principalReady, ready, refreshAll]);

  useEffect(() => {
    if (!credentials) return;
    void loadModelCatalog(runtimeKind, editingProfileName);
  }, [credentials, runtimeKind, editingProfileName, loadModelCatalog]);

  if (!principalReady) {
    return <main className="p-8 text-sm text-muted-foreground">Loading agent runtimes...</main>;
  }

  if (principal && !canAccessPlatformAdmin(principal)) {
    return <main className="p-8 text-sm text-muted-foreground">Redirecting...</main>;
  }

  return (
    <div className="mx-auto w-full max-w-6xl space-y-8">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-3xl font-semibold tracking-tight">Agent runtimes</h1>
          <p className="text-sm text-muted-foreground">
            Manage platform-wide routing and profile definitions. For worker health and heartbeat details, use{" "}
            <Link href="/status" className="text-primary underline-offset-4 hover:underline">
              platform status
            </Link>
            .
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => void refreshAll()}
          disabled={routingLoading || profilesLoading || toolsLoading || savingProfile || savingRouting}
        >
          <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${routingLoading || profilesLoading || toolsLoading ? "animate-spin" : ""}`} />
          Refresh
        </Button>
      </div>

      <div className="flex gap-2">
        {TAB_OPTIONS.map((tab) => (
          <Button key={tab.id} variant={activeTab === tab.id ? "default" : "outline"} size="sm" onClick={() => setActiveTab(tab.id)}>
            {tab.label}
          </Button>
        ))}
      </div>

      {activeTab === "routing" ? (
        <div className="space-y-6">
          {routingStatusLine ? (
            <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{routingStatusLine}</p>
          ) : null}

          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-3">
                  <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10">
                    <Bot className="h-5 w-5 text-primary" />
                  </div>
                  <div>
                    <CardTitle className="text-base">Runtime routing</CardTitle>
                    <p className="text-sm text-muted-foreground">
                      Named-agent overrides win over role defaults. Leave a row blank to inherit.
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Text <code className="rounded bg-muted px-1 py-0.5 font-mono text-[11px]">!pm</code> uses{" "}
                      <span className="font-mono">pm_primary</span> (built-in default profile{" "}
                      <span className="font-mono">pm_conversation_default</span>). Routed voice PM uses{" "}
                      <span className="font-mono">voice_room_pm</span> (default{" "}
                      <span className="font-mono">pm_conversation_fast</span>).
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Selector rows are separate (for values like <code>discord.voice_room_router</code>) and are the correct place to change
                      selector-driven routing.
                    </p>
                  </div>
                </div>
                <div className="flex gap-2">
                  <Button variant="outline" size="sm" onClick={() => void resetRouting()} disabled={savingRouting || routingLoading}>
                    <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
                    Reset
                  </Button>
                  <Button size="sm" onClick={() => void saveRouting()} disabled={savingRouting || routingLoading || !routing}>
                    <Save className="mr-1.5 h-3.5 w-3.5" />
                    Save
                  </Button>
                </div>
              </div>
            </CardHeader>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">Execution selectors</CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Selector</TableHead>
                    <TableHead>Override</TableHead>
                    <TableHead>Default</TableHead>
                    <TableHead>Effective profile</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(routing?.available_selectors ?? []).map((selector) => {
                    const selectedRuntime = selectorRouting[selector] ?? "";
                    const fallbackProfile = routing?.effective_defaults.selector_routing[selector] ?? "";
                    const effectiveProfileName = selectedRuntime || fallbackProfile;
                    return (
                      <TableRow key={selector}>
                        <TableCell className="font-medium">{selector}</TableCell>
                        <TableCell className="min-w-[240px]">
                          <select
                            className="h-9 w-full rounded border border-input bg-background px-3 text-sm"
                            value={selectedRuntime}
                            onChange={(event) => setSelectorRouting((current) => ({ ...current, [selector]: event.target.value }))}
                            disabled={routingLoading || savingRouting}
                          >
                            <option value="">Inherit default</option>
                            {sortedProfileNames.map((profileName) => (
                              <option key={profileName} value={profileName}>
                                {profileName}
                              </option>
                            ))}
                          </select>
                        </TableCell>
                        <TableCell className="font-mono text-xs">{fallbackProfile || "—"}</TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          <p className="font-mono text-foreground">{effectiveProfileName || "—"}</p>
                          <p>{profileSummary(profiles[effectiveProfileName])}</p>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">Role defaults</CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Role</TableHead>
                    <TableHead>Override</TableHead>
                    <TableHead>Default</TableHead>
                    <TableHead>Effective profile</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(routing?.available_roles ?? []).map((role) => {
                    const selectedRuntime = roleRouting[role] ?? "";
                    const fallbackProfile = routing?.effective_defaults.role_routing[role] ?? "";
                    const effectiveProfileName = selectedRuntime || fallbackProfile;
                    return (
                      <TableRow key={role}>
                        <TableCell className="font-medium">{role}</TableCell>
                        <TableCell className="min-w-[240px]">
                          <select
                            className="h-9 w-full rounded border border-input bg-background px-3 text-sm"
                            value={selectedRuntime}
                            onChange={(event) => setRoleRouting((current) => ({ ...current, [role]: event.target.value }))}
                            disabled={routingLoading || savingRouting}
                          >
                            <option value="">Inherit default</option>
                            {sortedProfileNames.map((profileName) => (
                              <option key={profileName} value={profileName}>
                                {profileName}
                              </option>
                            ))}
                          </select>
                        </TableCell>
                        <TableCell className="font-mono text-xs">{fallbackProfile || "—"}</TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          <p className="font-mono text-foreground">{effectiveProfileName || "—"}</p>
                          <p>{profileSummary(profiles[effectiveProfileName])}</p>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">Named-agent overrides</CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Named agent</TableHead>
                    <TableHead>Override</TableHead>
                    <TableHead>Default</TableHead>
                    <TableHead>Effective profile</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(routing?.available_named_agents ?? []).map((agentName) => {
                    const selectedRuntime = nameRouting[agentName] ?? "";
                    const fallbackProfile = routing?.effective_defaults.name_routing[agentName] ?? "";
                    const effectiveProfileName = selectedRuntime || fallbackProfile;
                    return (
                      <TableRow key={agentName}>
                        <TableCell className="font-medium">{agentName}</TableCell>
                        <TableCell className="min-w-[240px]">
                          <select
                            className="h-9 w-full rounded border border-input bg-background px-3 text-sm"
                            value={selectedRuntime}
                            onChange={(event) => setNameRouting((current) => ({ ...current, [agentName]: event.target.value }))}
                            disabled={routingLoading || savingRouting}
                          >
                            <option value="">Inherit default</option>
                            {sortedProfileNames.map((profileName) => (
                              <option key={profileName} value={profileName}>
                                {profileName}
                              </option>
                            ))}
                          </select>
                        </TableCell>
                        <TableCell className="font-mono text-xs">{fallbackProfile || "—"}</TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          <p className="font-mono text-foreground">{effectiveProfileName || "—"}</p>
                          <p>{profileSummary(profiles[effectiveProfileName])}</p>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </div>
      ) : activeTab === "profiles" ? (
        <div className="grid gap-6 lg:grid-cols-[1.4fr,1fr]">
          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <CardTitle className="text-base">Profiles</CardTitle>
                  <p className="text-sm text-muted-foreground">
                    Built-ins can be edited and reset. Custom profiles can be created and deleted.
                  </p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setEditingProfileName(null);
                    setDraft(buildDraft());
                    setProfilesStatusLine("Creating a new profile.");
                  }}
                >
                  <Plus className="mr-1.5 h-3.5 w-3.5" />
                  New profile
                </Button>
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              {profilesStatusLine ? (
                <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{profilesStatusLine}</p>
              ) : null}
              <div className="rounded-md border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Profile</TableHead>
                      <TableHead>Runtime</TableHead>
                      <TableHead>Model</TableHead>
                      <TableHead>Usage</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {sortedProfileNames.map((profileName) => {
                      const profile = profiles[profileName];
                      return (
                        <TableRow
                          key={profileName}
                          className={editingProfileName === profileName ? "bg-muted/40" : ""}
                          onClick={() => {
                            setEditingProfileName(profileName);
                            setDraft(buildDraft(profile));
                          }}
                        >
                          <TableCell className="space-y-1">
                            <p className="font-mono text-xs">{profile.profile_name}</p>
                            <div className="flex gap-1">
                              <Badge variant={profile.is_builtin ? "secondary" : "outline"}>{profile.is_builtin ? "Built-in" : "Custom"}</Badge>
                              {profile.is_overridden ? <Badge variant="outline">Overridden</Badge> : null}
                            </div>
                          </TableCell>
                          <TableCell>{profile.runtime_kind}</TableCell>
                          <TableCell className="font-mono text-xs">{profile.model}</TableCell>
                          <TableCell className="text-xs text-muted-foreground">
                            {profile.usage_references.length > 0 ? profile.usage_references.join(", ") : "Unused"}
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <CardTitle className="text-base">{editingProfileName ? `Edit ${editingProfileName}` : "Create profile"}</CardTitle>
                  <p className="text-sm text-muted-foreground">
                    Secret refs should point to Platform Secrets. Tenant and project model pickers will follow the engineering execution runtime.
                  </p>
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Profile name</label>
                <Input
                  aria-label="Profile name"
                  value={draft.profile_name}
                  onChange={(event) => setDraft((current) => ({ ...current, profile_name: event.target.value }))}
                  disabled={Boolean(editingProfileName)}
                  placeholder="openai_engineering_fast"
                />
              </div>

              <div className="space-y-2">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Runtime</label>
                <select
                  aria-label="Runtime"
                  className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                  value={draft.runtime_kind}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...applyRuntimeTransportDefaults(current, event.target.value),
                      reasoning_effort: null,
                    }))
                  }
                >
                  {RUNTIME_OPTIONS.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </div>

              {currentTransportIsCli ? (
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">CLI command</label>
                  <Input
                    aria-label="CLI command"
                    value={draft.cli_command}
                    onChange={(event) => setDraft((current) => ({ ...current, cli_command: event.target.value }))}
                    placeholder={runtimeKind === "claude_cli" ? "claude" : "codex"}
                  />
                </div>
              ) : null}

              {currentTransportNeedsBaseUrl || runtimeKind === "openai" || runtimeKind === "claude" ? (
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Base URL</label>
                  <Input
                    aria-label="Base URL"
                    value={draft.base_url ?? ""}
                    onChange={(event) => setDraft((current) => ({ ...current, base_url: event.target.value || null }))}
                    placeholder={
                      runtimeKind === "openai"
                        ? "https://api.openai.com/v1"
                        : runtimeKind === "claude"
                          ? "https://api.anthropic.com"
                          : "http://localhost:1234/v1"
                    }
                  />
                </div>
              ) : null}

              {currentTransportAllowsApiKey ? (
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">API key secret ref</label>
                  <Input
                    aria-label="API key secret ref"
                    value={draft.api_key_secret_ref ?? ""}
                    onChange={(event) => setDraft((current) => ({ ...current, api_key_secret_ref: event.target.value || null }))}
                    placeholder="platform/openai_api_key"
                  />
                </div>
              ) : null}

              <div className="space-y-2">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Model</label>
                <CodexModelSelect
                  ariaLabel="Model"
                  editorSurfaceKey={editingProfileName ?? "__create_profile__"}
                  value={draft.model}
                  models={modelCatalog?.models ?? []}
                  inheritLabel="Select model"
                  helperText="Presets come from this profile and other profiles using the same runtime. The value saved here is sent to the server as the OpenAI-style model id (must match LM Studio’s loaded model). Text Discord !pm uses the profile routed for pm_primary (default pm_conversation_default), not necessarily the row you are editing."
                  disabled={savingProfile}
                  onChange={(value) => setDraft((current) => ({ ...current, model: value }))}
                />
              </div>

              <div className="space-y-2">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Fallback profile</label>
                <select
                  aria-label="Fallback profile"
                  className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                  value={draft.fallback_profile ?? ""}
                  onChange={(event) => setDraft((current) => ({ ...current, fallback_profile: event.target.value || null }))}
                >
                  <option value="">No fallback</option>
                  {sortedProfileNames
                    .filter((profileName) => profileName !== draft.profile_name)
                    .map((profileName) => (
                      <option key={profileName} value={profileName}>
                        {profileName}
                      </option>
                    ))}
                </select>
              </div>

              <div className="space-y-2">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Reasoning mode</label>
                <select
                  aria-label="Reasoning mode"
                  className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                  value={draft.reasoning_effort ?? ""}
                  disabled={!currentReasoningSupported}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      reasoning_effort: (event.target.value || null) as "low" | "medium" | "high" | null,
                    }))
                  }
                >
                  <option value="">{currentReasoningSupported ? "No override" : "Not supported by this runtime"}</option>
                  {reasoningEfforts.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </div>

              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  className="h-4 w-4 rounded border-input"
                  checked={draft.tool_bridge_allowed}
                  onChange={(event) => setDraft((current) => ({ ...current, tool_bridge_allowed: event.target.checked }))}
                />
                Tool bridge allowed
              </label>

              {selectedProfile?.usage_references?.length ? (
                <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-xs text-muted-foreground">
                  In use: {selectedProfile.usage_references.join(", ")}
                </p>
              ) : null}

              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  onClick={() => void saveProfile()}
                  disabled={
                    savingProfile ||
                    !draft.profile_name.trim() ||
                    !String(draft.model || "").trim() ||
                    missingRequiredTransportField
                  }
                >
                  <Save className="mr-1.5 h-3.5 w-3.5" />
                  {editingProfileName ? "Save profile" : "Create profile"}
                </Button>
                {selectedProfile?.can_reset ? (
                  <Button variant="outline" size="sm" onClick={() => void resetProfile()} disabled={savingProfile}>
                    <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
                    Reset to default
                  </Button>
                ) : null}
                {editingProfileName && !selectedProfile?.is_builtin ? (
                  <Button variant="outline" size="sm" onClick={() => void deleteProfile()} disabled={savingProfile || !selectedProfile?.can_delete}>
                    <Trash2 className="mr-1.5 h-3.5 w-3.5" />
                    Delete
                  </Button>
                ) : null}
              </div>
            </CardContent>
          </Card>
        </div>
      ) : (
        <div className="space-y-6">
          {toolsStatusLine ? (
            <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{toolsStatusLine}</p>
          ) : null}

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">Implemented tools</CardTitle>
              <p className="text-sm text-muted-foreground">
                Read-only list of the governed tools available in the runtime bridge and the workflow stages that can call them.
              </p>
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-xs text-muted-foreground">
                {tools.length} tools across {(toolsResponse?.available_stages ?? []).length} stages.
              </p>
              <div className="rounded-md border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Tool</TableHead>
                      <TableHead>Category</TableHead>
                      <TableHead>Stages</TableHead>
                      <TableHead>Description</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {tools.map((tool: AgentRuntimeToolRecord) => (
                      <TableRow key={tool.tool_name}>
                        <TableCell className="font-mono text-xs">{tool.tool_name}</TableCell>
                        <TableCell>{tool.category}</TableCell>
                        <TableCell className="text-xs text-muted-foreground">{tool.stages.join(", ")}</TableCell>
                        <TableCell className="text-sm text-muted-foreground">{tool.description}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
