"use client";

import { useEffect, useMemo, useState } from "react";
import { Bot, RefreshCw, RotateCcw, Save } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  getAgentRuntimeRouting,
  resetAgentRuntimeRouting,
  updateAgentRuntimeRouting,
  type AgentExecutionProfileRecord,
  type AgentRuntimeRoutingRecord,
} from "@/lib/api";

function profileSummary(profile: AgentExecutionProfileRecord | undefined): string {
  if (!profile) return "No profile selected";
  const parts = [
    profile.runtime_kind,
    profile.cli_command,
    profile.model,
    profile.reasoning_effort ?? "no reasoning override",
    profile.tool_bridge_allowed ? "tools on" : "tools off",
  ];
  if (profile.fallback_profile) {
    parts.push(`fallback ${profile.fallback_profile}`);
  }
  return parts.join(" · ");
}

export default function AgentRuntimesPage() {
  const { credentials } = useAuth();
  const [routing, setRouting] = useState<AgentRuntimeRoutingRecord | null>(null);
  const [roleRouting, setRoleRouting] = useState<Record<string, string>>({});
  const [nameRouting, setNameRouting] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [statusLine, setStatusLine] = useState("");

  async function refresh(): Promise<void> {
    if (!credentials) return;
    setLoading(true);
    try {
      const response = await getAgentRuntimeRouting(credentials);
      setRouting(response);
      setRoleRouting(response.role_routing);
      setNameRouting(response.name_routing);
      setStatusLine("Loaded platform agent runtime routing.");
    } catch (error) {
      setStatusLine(`Failed to load agent runtimes: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, [credentials]); // eslint-disable-line react-hooks/exhaustive-deps

  const profiles = routing?.available_profiles ?? {};
  const sortedProfileNames = useMemo(() => Object.keys(profiles).sort(), [profiles]);

  async function save(): Promise<void> {
    if (!credentials) return;
    setSaving(true);
    try {
      const response = await updateAgentRuntimeRouting(credentials, {
        role_routing: Object.fromEntries(Object.entries(roleRouting).filter(([, value]) => value)),
        name_routing: Object.fromEntries(Object.entries(nameRouting).filter(([, value]) => value)),
      });
      setRouting(response);
      setRoleRouting(response.role_routing);
      setNameRouting(response.name_routing);
      setStatusLine("Saved platform agent runtime routing.");
    } catch (error) {
      setStatusLine(`Save failed: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function reset(): Promise<void> {
    if (!credentials) return;
    if (!window.confirm("Reset all platform agent runtime overrides?")) return;
    setSaving(true);
    try {
      const response = await resetAgentRuntimeRouting(credentials);
      setRouting(response);
      setRoleRouting(response.role_routing);
      setNameRouting(response.name_routing);
      setStatusLine("Reset platform agent runtime routing to inherited defaults.");
    } catch (error) {
      setStatusLine(`Reset failed: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10">
            <Bot className="h-5 w-5 text-primary" />
          </div>
          <div>
            <h1 className="text-xl font-semibold">Agent Runtimes</h1>
            <p className="text-sm text-muted-foreground">
              Manage platform-wide routing from agent roles and named agents to built-in runtime profiles.
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={loading || saving}>
            <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </Button>
          <Button variant="outline" size="sm" onClick={() => void reset()} disabled={loading || saving}>
            <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
            Reset
          </Button>
          <Button size="sm" onClick={() => void save()} disabled={loading || saving || !routing}>
            <Save className="mr-1.5 h-3.5 w-3.5" />
            Save
          </Button>
        </div>
      </div>

      {statusLine ? (
        <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p>
      ) : null}

      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between gap-2">
            <CardTitle className="text-base">Routing precedence</CardTitle>
            {routing ? (
              <Badge variant="outline" className="text-xs">
                named agent overrides role, role overrides selector/default
              </Badge>
            ) : null}
          </div>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Use a named-agent override only when one specific agent needs a different runtime. Leave a row blank to
          inherit the lower-precedence default.
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
                const selectedProfile = roleRouting[role] ?? "";
                const fallbackProfile = routing?.effective_defaults.role_routing[role] ?? "";
                const effectiveProfileName = selectedProfile || fallbackProfile;
                return (
                  <TableRow key={role}>
                    <TableCell className="font-medium">{role}</TableCell>
                    <TableCell className="min-w-[240px]">
                      <select
                        className="h-9 w-full rounded border border-input bg-background px-3 text-sm"
                        value={selectedProfile}
                        onChange={(e) => setRoleRouting((current) => ({ ...current, [role]: e.target.value }))}
                        disabled={loading || saving}
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
                const selectedProfile = nameRouting[agentName] ?? "";
                const fallbackProfile = routing?.effective_defaults.name_routing[agentName] ?? "";
                const effectiveProfileName = selectedProfile || fallbackProfile;
                return (
                  <TableRow key={agentName}>
                    <TableCell className="font-medium">{agentName}</TableCell>
                    <TableCell className="min-w-[240px]">
                      <select
                        className="h-9 w-full rounded border border-input bg-background px-3 text-sm"
                        value={selectedProfile}
                        onChange={(e) => setNameRouting((current) => ({ ...current, [agentName]: e.target.value }))}
                        disabled={loading || saving}
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
          <CardTitle className="text-base">Available profiles</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Profile</TableHead>
                <TableHead>Runtime</TableHead>
                <TableHead>CLI</TableHead>
                <TableHead>Model</TableHead>
                <TableHead>Reasoning</TableHead>
                <TableHead>Fallback</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sortedProfileNames.map((profileName) => {
                const profile = profiles[profileName];
                return (
                  <TableRow key={profileName}>
                    <TableCell className="font-mono text-xs">{profileName}</TableCell>
                    <TableCell>{profile.runtime_kind}</TableCell>
                    <TableCell className="font-mono text-xs">{profile.cli_command}</TableCell>
                    <TableCell className="font-mono text-xs">{profile.model}</TableCell>
                    <TableCell>{profile.reasoning_effort ?? "—"}</TableCell>
                    <TableCell className="font-mono text-xs">{profile.fallback_profile ?? "—"}</TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
