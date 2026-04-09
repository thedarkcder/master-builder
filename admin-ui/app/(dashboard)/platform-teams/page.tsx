"use client";

import { useEffect, useMemo, useState } from "react";
import { Bot, Network, RefreshCw, Save, Sparkles, Users } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import {
  canAccessPlatformAdmin,
  getDefaultAuthenticatedRoute,
} from "@/lib/auth-routing";
import {
  createPlatformAgent,
  createPlatformPersona,
  createPlatformTeamTemplate,
  getPlatformRuntimeBindings,
  listPlatformAgents,
  listPlatformPersonas,
  listPlatformTeamTemplates,
  publishPlatformTeamTemplate,
  updatePlatformAgent,
  updatePlatformPersona,
  updatePlatformTeamTemplate,
  type PlatformAgentRecord,
  type PlatformAgentWritePayload,
  type PlatformPersonaRecord,
  type PlatformPersonaWritePayload,
  type PlatformRuntimeBindingsRecord,
  type PlatformTeamTemplateRecord,
  type PlatformTeamTemplateWritePayload,
} from "@/lib/api";

type CatalogTab = "teams" | "personas" | "agents" | "runtime";

type PersonaDraft = PlatformPersonaWritePayload;

type AgentDraft = PlatformAgentWritePayload;

type TeamRoleDraft = {
  role_key: string;
  label: string;
  description: string;
  position: number;
  persona_key: string;
  agent_key: string;
};

type TeamTaskDraft = {
  task_key: string;
  label: string;
  owner_role_key: string;
  position: number;
  produces: string;
  consumes: string;
  approval_type: string;
};

type TeamEdgeDraft = {
  from_task_key: string;
  to_task_key: string;
};

type TeamTemplateDraft = {
  team_key: string;
  label: string;
  description: string;
  is_active: boolean;
  roles: TeamRoleDraft[];
  tasks: TeamTaskDraft[];
  edges: TeamEdgeDraft[];
};

const TAB_OPTIONS: { id: CatalogTab; label: string; icon: typeof Users }[] = [
  { id: "teams", label: "Teams", icon: Users },
  { id: "personas", label: "Personas", icon: Sparkles },
  { id: "agents", label: "Agents", icon: Bot },
  { id: "runtime", label: "Runtime bindings", icon: Network },
];

function splitCsv(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

function joinCsv(values: string[] | null | undefined): string {
  return (values ?? []).join(", ");
}

function emptyPersonaDraft(): PersonaDraft {
  return {
    persona_key: "",
    label: "",
    description: "",
    default_display_name: "",
    default_voice_id: "",
    system_prompt_template: "",
    user_prompt_template: "",
    allowed_surfaces: [],
    is_active: true,
  };
}

function emptyAgentDraft(personaKey = ""): AgentDraft {
  return {
    agent_key: "",
    label: "",
    description: "",
    persona_key: personaKey,
    runtime_role_key: "",
    named_agent_key: "",
    selector_key: "",
    default_profile_name: "",
    is_active: true,
  };
}

function emptyRoleDraft(): TeamRoleDraft {
  return {
    role_key: "",
    label: "",
    description: "",
    position: 1,
    persona_key: "",
    agent_key: "",
  };
}

function emptyTaskDraft(position = 1): TeamTaskDraft {
  return {
    task_key: "",
    label: "",
    owner_role_key: "",
    position,
    produces: "",
    consumes: "",
    approval_type: "",
  };
}

function emptyTeamDraft(): TeamTemplateDraft {
  return {
    team_key: "",
    label: "",
    description: "",
    is_active: true,
    roles: [emptyRoleDraft()],
    tasks: [emptyTaskDraft(1)],
    edges: [],
  };
}

function personaToDraft(persona: PlatformPersonaRecord): PersonaDraft {
  return {
    persona_key: persona.persona_key,
    label: persona.label,
    description: persona.description ?? "",
    default_display_name: persona.default_display_name ?? "",
    default_voice_id: persona.default_voice_id ?? "",
    system_prompt_template: persona.system_prompt_template ?? "",
    user_prompt_template: persona.user_prompt_template ?? "",
    allowed_surfaces: persona.allowed_surfaces ?? [],
    is_active: persona.is_active,
  };
}

function agentToDraft(agent: PlatformAgentRecord): AgentDraft {
  return {
    agent_key: agent.agent_key,
    label: agent.label,
    description: agent.description ?? "",
    persona_key: agent.persona_key,
    runtime_role_key: agent.runtime_role_key ?? "",
    named_agent_key: agent.named_agent_key ?? "",
    selector_key: agent.selector_key ?? "",
    default_profile_name: agent.default_profile_name ?? "",
    is_active: agent.is_active,
  };
}

function teamToDraft(template: PlatformTeamTemplateRecord): TeamTemplateDraft {
  return {
    team_key: template.team_key,
    label: template.team_label,
    description: template.description ?? "",
    is_active: template.is_active,
    roles: template.roles.map((role) => ({
      role_key: role.role_key,
      label: role.label,
      description: role.description ?? "",
      position: role.position,
      persona_key: role.persona.persona_key,
      agent_key: role.agent.agent_key,
    })),
    tasks: template.tasks.map((task) => ({
      task_key: task.task_key,
      label: task.label,
      owner_role_key: task.owner_role_key,
      position: task.position,
      produces: joinCsv((task.artifact_contract?.produces as string[] | undefined) ?? []),
      consumes: joinCsv((task.artifact_contract?.consumes as string[] | undefined) ?? []),
      approval_type: String(task.approval_rule?.type ?? ""),
    })),
    edges: template.edges.map((edge) => ({
      from_task_key: edge.from_task_key,
      to_task_key: edge.to_task_key,
    })),
  };
}

function teamDraftToPayload(draft: TeamTemplateDraft): PlatformTeamTemplateWritePayload {
  return {
    team_key: draft.team_key.trim(),
    label: draft.label.trim(),
    description: draft.description.trim() || null,
    is_active: draft.is_active,
    roles: draft.roles
      .filter((role) => role.role_key.trim() && role.label.trim() && role.persona_key.trim() && role.agent_key.trim())
      .map((role) => ({
        role_key: role.role_key.trim(),
        label: role.label.trim(),
        description: role.description.trim() || null,
        position: role.position,
        persona_key: role.persona_key.trim(),
        agent_key: role.agent_key.trim(),
      })),
    tasks: draft.tasks
      .filter((task) => task.task_key.trim() && task.label.trim() && task.owner_role_key.trim())
      .map((task) => ({
        task_key: task.task_key.trim(),
        label: task.label.trim(),
        owner_role_key: task.owner_role_key.trim(),
        position: task.position,
        artifact_contract: {
          ...(splitCsv(task.produces).length > 0 ? { produces: splitCsv(task.produces) } : {}),
          ...(splitCsv(task.consumes).length > 0 ? { consumes: splitCsv(task.consumes) } : {}),
        },
        approval_rule: task.approval_type.trim() ? { type: task.approval_type.trim() } : {},
      })),
    edges: draft.edges
      .filter((edge) => edge.from_task_key.trim() && edge.to_task_key.trim())
      .map((edge) => ({
        from_task_key: edge.from_task_key.trim(),
        to_task_key: edge.to_task_key.trim(),
      })),
  };
}

function CatalogListHeader({
  title,
  description,
  count,
}: {
  title: string;
  description: string;
  count: number;
}) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div>
        <h2 className="text-base font-semibold">{title}</h2>
        <p className="text-sm text-muted-foreground">{description}</p>
      </div>
      <Badge variant="outline">{count}</Badge>
    </div>
  );
}

export default function PlatformTeamsPage() {
  const { credentials, principal, principalReady, ready } = useAuth();
  const [activeTab, setActiveTab] = useState<CatalogTab>("teams");
  const [personas, setPersonas] = useState<PlatformPersonaRecord[]>([]);
  const [agents, setAgents] = useState<PlatformAgentRecord[]>([]);
  const [templates, setTemplates] = useState<PlatformTeamTemplateRecord[]>([]);
  const [runtimeBindings, setRuntimeBindings] = useState<PlatformRuntimeBindingsRecord | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [statusLine, setStatusLine] = useState("");

  const [selectedPersonaId, setSelectedPersonaId] = useState<string | null>(null);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [selectedTemplateId, setSelectedTemplateId] = useState<string | null>(null);

  const [personaDraft, setPersonaDraft] = useState<PersonaDraft>(() => emptyPersonaDraft());
  const [personaSurfacesText, setPersonaSurfacesText] = useState("");
  const [agentDraft, setAgentDraft] = useState<AgentDraft>(() => emptyAgentDraft());
  const [teamDraft, setTeamDraft] = useState<TeamTemplateDraft>(() => emptyTeamDraft());

  const personaOptions = useMemo(
    () => personas.slice().sort((left, right) => left.label.localeCompare(right.label)),
    [personas],
  );
  const agentOptions = useMemo(
    () => agents.slice().sort((left, right) => left.label.localeCompare(right.label)),
    [agents],
  );
  const templateOptions = useMemo(
    () => templates.slice().sort((left, right) => left.team_label.localeCompare(right.team_label)),
    [templates],
  );

  async function loadAll(): Promise<void> {
    if (!credentials) {
      return;
    }
    setLoading(true);
    try {
      const [personaPayload, agentPayload, templatePayload, runtimePayload] = await Promise.all([
        listPlatformPersonas(credentials),
        listPlatformAgents(credentials),
        listPlatformTeamTemplates(credentials),
        getPlatformRuntimeBindings(credentials),
      ]);
      setPersonas(personaPayload);
      setAgents(agentPayload);
      setTemplates(templatePayload);
      setRuntimeBindings(runtimePayload);
      setStatusLine("Loaded platform team catalog.");
      if (!selectedPersonaId && personaPayload.length > 0) {
        setSelectedPersonaId(personaPayload[0].persona_id);
        setPersonaDraft(personaToDraft(personaPayload[0]));
        setPersonaSurfacesText(joinCsv(personaPayload[0].allowed_surfaces));
      }
      if (!selectedAgentId && agentPayload.length > 0) {
        setSelectedAgentId(agentPayload[0].agent_id);
        setAgentDraft(agentToDraft(agentPayload[0]));
      }
      if (!selectedTemplateId && templatePayload.length > 0) {
        setSelectedTemplateId(templatePayload[0].template_id);
        setTeamDraft(teamToDraft(templatePayload[0]));
      }
    } catch (error) {
      setStatusLine(`Failed to load platform team catalog: ${(error as Error).message}`);
    } finally {
      setLoading(false);
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
      void loadAll();
    }
  }, [credentials, principal, principalReady, ready]);

  if (!principalReady) {
    return <main className="p-8 text-sm text-muted-foreground">Loading platform catalog...</main>;
  }

  if (principal && !canAccessPlatformAdmin(principal)) {
    return <main className="p-8 text-sm text-muted-foreground">Redirecting...</main>;
  }

  async function savePersona(): Promise<void> {
    if (!credentials) return;
    setSaving(true);
    try {
      const payload: PlatformPersonaWritePayload = {
        ...personaDraft,
        persona_key: String(personaDraft.persona_key ?? "").trim(),
        label: String(personaDraft.label ?? "").trim(),
        description: personaDraft.description?.trim() || null,
        default_display_name: personaDraft.default_display_name?.trim() || null,
        default_voice_id: personaDraft.default_voice_id?.trim() || null,
        system_prompt_template: personaDraft.system_prompt_template?.trim() || null,
        user_prompt_template: personaDraft.user_prompt_template?.trim() || null,
        allowed_surfaces: splitCsv(personaSurfacesText),
      };
      const saved = selectedPersonaId
        ? await updatePlatformPersona(credentials, selectedPersonaId, payload)
        : await createPlatformPersona(credentials, payload);
      setSelectedPersonaId(saved.persona_id);
      setPersonaDraft(personaToDraft(saved));
      setPersonaSurfacesText(joinCsv(saved.allowed_surfaces));
      setStatusLine(`Saved persona ${saved.label}.`);
      await loadAll();
    } catch (error) {
      setStatusLine(`Failed to save persona: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function saveAgent(): Promise<void> {
    if (!credentials) return;
    setSaving(true);
    try {
      const payload: PlatformAgentWritePayload = {
        ...agentDraft,
        agent_key: String(agentDraft.agent_key ?? "").trim(),
        label: String(agentDraft.label ?? "").trim(),
        description: agentDraft.description?.trim() || null,
        runtime_role_key: agentDraft.runtime_role_key?.trim() || null,
        named_agent_key: agentDraft.named_agent_key?.trim() || null,
        selector_key: agentDraft.selector_key?.trim() || null,
        default_profile_name: agentDraft.default_profile_name?.trim() || null,
      };
      const saved = selectedAgentId
        ? await updatePlatformAgent(credentials, selectedAgentId, payload)
        : await createPlatformAgent(credentials, payload);
      setSelectedAgentId(saved.agent_id);
      setAgentDraft(agentToDraft(saved));
      setStatusLine(`Saved agent ${saved.label}.`);
      await loadAll();
    } catch (error) {
      setStatusLine(`Failed to save agent: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function saveTeamTemplate(): Promise<void> {
    if (!credentials) return;
    setSaving(true);
    try {
      const payload = teamDraftToPayload(teamDraft);
      const saved = selectedTemplateId
        ? await updatePlatformTeamTemplate(credentials, selectedTemplateId, payload)
        : await createPlatformTeamTemplate(credentials, payload);
      setSelectedTemplateId(saved.template_id);
      setTeamDraft(teamToDraft(saved));
      setStatusLine(`Saved team template ${saved.team_label}.`);
      await loadAll();
    } catch (error) {
      setStatusLine(`Failed to save team template: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function publishTemplate(): Promise<void> {
    if (!credentials || !selectedTemplateId) return;
    setSaving(true);
    try {
      const saved = await publishPlatformTeamTemplate(credentials, selectedTemplateId);
      setTeamDraft(teamToDraft(saved));
      setStatusLine(`Published team template ${saved.team_label} v${saved.definition_version}.`);
      await loadAll();
    } catch (error) {
      setStatusLine(`Failed to publish team template: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  const selectedTemplateRecord = templates.find((template) => template.template_id === selectedTemplateId) ?? null;

  return (
    <div className="mx-auto w-full max-w-7xl space-y-8">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-3xl font-semibold tracking-tight">Platform team catalog</h1>
          <p className="text-sm text-muted-foreground">
            Manage team templates, personas, agents, and the runtime bindings they publish into the platform.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void loadAll()} disabled={loading || saving}>
          <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </Button>
      </div>

      <div className="flex gap-2 overflow-x-auto">
        {TAB_OPTIONS.map((tab) => {
          const Icon = tab.icon;
          return (
            <Button key={tab.id} variant={activeTab === tab.id ? "default" : "outline"} size="sm" onClick={() => setActiveTab(tab.id)}>
              <Icon className="mr-1.5 h-3.5 w-3.5" />
              {tab.label}
            </Button>
          );
        })}
      </div>

      {statusLine ? (
        <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p>
      ) : null}

      {activeTab === "personas" ? (
        <div className="grid gap-6 lg:grid-cols-[1.1fr,1fr]">
          <Card>
            <CardHeader className="pb-3">
              <CatalogListHeader title="Personas" description="Behavior and surface defaults for human-facing identities." count={personas.length} />
            </CardHeader>
            <CardContent className="p-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Persona</TableHead>
                    <TableHead>Surfaces</TableHead>
                    <TableHead>Version</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {personaOptions.map((persona) => (
                    <TableRow
                      key={persona.persona_id}
                      className={selectedPersonaId === persona.persona_id ? "bg-muted/40" : ""}
                      onClick={() => {
                        setSelectedPersonaId(persona.persona_id);
                        setPersonaDraft(personaToDraft(persona));
                        setPersonaSurfacesText(joinCsv(persona.allowed_surfaces));
                      }}
                    >
                      <TableCell>
                        <p className="font-medium">{persona.label}</p>
                        <p className="font-mono text-xs text-muted-foreground">{persona.persona_key}</p>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {persona.allowed_surfaces.length > 0 ? persona.allowed_surfaces.join(", ") : "No surface bindings"}
                      </TableCell>
                      <TableCell>v{persona.version}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">{selectedPersonaId ? "Edit persona" : "Create persona"}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 md:grid-cols-2">
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Persona key</label>
                  <Input value={personaDraft.persona_key ?? ""} disabled={Boolean(selectedPersonaId)} onChange={(event) => setPersonaDraft((current) => ({ ...current, persona_key: event.target.value }))} />
                </div>
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Label</label>
                  <Input value={personaDraft.label} onChange={(event) => setPersonaDraft((current) => ({ ...current, label: event.target.value }))} />
                </div>
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Display name</label>
                  <Input value={personaDraft.default_display_name ?? ""} onChange={(event) => setPersonaDraft((current) => ({ ...current, default_display_name: event.target.value }))} />
                </div>
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Default voice</label>
                  <Input value={personaDraft.default_voice_id ?? ""} onChange={(event) => setPersonaDraft((current) => ({ ...current, default_voice_id: event.target.value }))} />
                </div>
              </div>
              <div className="space-y-2">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Allowed surfaces</label>
                <Input value={personaSurfacesText} onChange={(event) => setPersonaSurfacesText(event.target.value)} placeholder="discord_voice_room, team_run_execution" />
              </div>
              <div className="space-y-2">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Description</label>
                <Textarea value={personaDraft.description ?? ""} onChange={(event) => setPersonaDraft((current) => ({ ...current, description: event.target.value }))} className="min-h-[88px]" />
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">System prompt template</label>
                  <Textarea value={personaDraft.system_prompt_template ?? ""} onChange={(event) => setPersonaDraft((current) => ({ ...current, system_prompt_template: event.target.value }))} className="min-h-[110px]" />
                </div>
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">User prompt template</label>
                  <Textarea value={personaDraft.user_prompt_template ?? ""} onChange={(event) => setPersonaDraft((current) => ({ ...current, user_prompt_template: event.target.value }))} className="min-h-[110px]" />
                </div>
              </div>
              <div className="flex items-center justify-between">
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={personaDraft.is_active} onChange={(event) => setPersonaDraft((current) => ({ ...current, is_active: event.target.checked }))} />
                  Active
                </label>
                <div className="flex gap-2">
                  <Button variant="outline" size="sm" onClick={() => { setSelectedPersonaId(null); setPersonaDraft(emptyPersonaDraft()); setPersonaSurfacesText(""); }}>
                    New
                  </Button>
                  <Button size="sm" onClick={() => void savePersona()} disabled={saving || !String(personaDraft.persona_key ?? "").trim() || !String(personaDraft.label ?? "").trim()}>
                    <Save className="mr-1.5 h-3.5 w-3.5" />
                    Save persona
                  </Button>
                </div>
              </div>
            </CardContent>
          </Card>
        </div>
      ) : null}

      {activeTab === "agents" ? (
        <div className="grid gap-6 lg:grid-cols-[1.1fr,1fr]">
          <Card>
            <CardHeader className="pb-3">
              <CatalogListHeader title="Agents" description="Execution identities bound to personas and runtime routes." count={agents.length} />
            </CardHeader>
            <CardContent className="p-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Agent</TableHead>
                    <TableHead>Persona</TableHead>
                    <TableHead>Binding</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {agentOptions.map((agent) => (
                    <TableRow
                      key={agent.agent_id}
                      className={selectedAgentId === agent.agent_id ? "bg-muted/40" : ""}
                      onClick={() => {
                        setSelectedAgentId(agent.agent_id);
                        setAgentDraft(agentToDraft(agent));
                      }}
                    >
                      <TableCell>
                        <p className="font-medium">{agent.label}</p>
                        <p className="font-mono text-xs text-muted-foreground">{agent.agent_key}</p>
                      </TableCell>
                      <TableCell>{agent.persona.label}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {agent.runtime_role_key || agent.named_agent_key || agent.selector_key || "No runtime binding"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">{selectedAgentId ? "Edit agent" : "Create agent"}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 md:grid-cols-2">
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Agent key</label>
                  <Input value={agentDraft.agent_key ?? ""} disabled={Boolean(selectedAgentId)} onChange={(event) => setAgentDraft((current) => ({ ...current, agent_key: event.target.value }))} />
                </div>
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Label</label>
                  <Input value={agentDraft.label} onChange={(event) => setAgentDraft((current) => ({ ...current, label: event.target.value }))} />
                </div>
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Persona</label>
                  <select className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm" value={agentDraft.persona_key} onChange={(event) => setAgentDraft((current) => ({ ...current, persona_key: event.target.value }))}>
                    <option value="">Select persona</option>
                    {personaOptions.map((persona) => (
                      <option key={persona.persona_id} value={persona.persona_key}>
                        {persona.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Default profile</label>
                  <Input value={agentDraft.default_profile_name ?? ""} onChange={(event) => setAgentDraft((current) => ({ ...current, default_profile_name: event.target.value }))} />
                </div>
              </div>
              <div className="grid gap-4 md:grid-cols-3">
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Runtime role key</label>
                  <Input value={agentDraft.runtime_role_key ?? ""} onChange={(event) => setAgentDraft((current) => ({ ...current, runtime_role_key: event.target.value }))} />
                </div>
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Named agent key</label>
                  <Input value={agentDraft.named_agent_key ?? ""} onChange={(event) => setAgentDraft((current) => ({ ...current, named_agent_key: event.target.value }))} />
                </div>
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Selector key</label>
                  <Input value={agentDraft.selector_key ?? ""} onChange={(event) => setAgentDraft((current) => ({ ...current, selector_key: event.target.value }))} />
                </div>
              </div>
              <div className="space-y-2">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Description</label>
                <Textarea value={agentDraft.description ?? ""} onChange={(event) => setAgentDraft((current) => ({ ...current, description: event.target.value }))} className="min-h-[96px]" />
              </div>
              <div className="flex items-center justify-between">
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={agentDraft.is_active} onChange={(event) => setAgentDraft((current) => ({ ...current, is_active: event.target.checked }))} />
                  Active
                </label>
                <div className="flex gap-2">
                  <Button variant="outline" size="sm" onClick={() => { setSelectedAgentId(null); setAgentDraft(emptyAgentDraft(personaOptions[0]?.persona_key ?? "")); }}>
                    New
                  </Button>
                  <Button size="sm" onClick={() => void saveAgent()} disabled={saving || !String(agentDraft.agent_key ?? "").trim() || !String(agentDraft.label ?? "").trim() || !String(agentDraft.persona_key ?? "").trim()}>
                    <Save className="mr-1.5 h-3.5 w-3.5" />
                    Save agent
                  </Button>
                </div>
              </div>
            </CardContent>
          </Card>
        </div>
      ) : null}

      {activeTab === "teams" ? (
        <div className="grid gap-6 xl:grid-cols-[1fr,1.4fr]">
          <Card>
            <CardHeader className="pb-3">
              <CatalogListHeader title="Team templates" description="Published graphs of roles, tasks, and handoffs." count={templates.length} />
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="rounded-md border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Template</TableHead>
                      <TableHead>Version</TableHead>
                      <TableHead>Status</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {templateOptions.map((template) => (
                      <TableRow
                        key={template.template_id}
                        className={selectedTemplateId === template.template_id ? "bg-muted/40" : ""}
                        onClick={() => {
                          setSelectedTemplateId(template.template_id);
                          setTeamDraft(teamToDraft(template));
                        }}
                      >
                        <TableCell>
                          <p className="font-medium">{template.team_label}</p>
                          <p className="font-mono text-xs text-muted-foreground">{template.team_key}</p>
                        </TableCell>
                        <TableCell>v{template.definition_version}</TableCell>
                        <TableCell>
                          <div className="flex flex-wrap gap-1">
                            <Badge variant={template.published_at ? "default" : "outline"}>
                              {template.published_at ? "Published" : "Draft"}
                            </Badge>
                            {!template.is_active ? <Badge variant="secondary">Inactive</Badge> : null}
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setSelectedTemplateId(null);
                    setTeamDraft(emptyTeamDraft());
                  }}
                >
                  New template
                </Button>
                <Button size="sm" onClick={() => void publishTemplate()} disabled={saving || !selectedTemplateId}>
                  Publish selected
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <CardTitle className="text-base">{selectedTemplateId ? "Edit team template" : "Create team template"}</CardTitle>
                  <p className="text-sm text-muted-foreground">
                    Build the graph from roles, tasks, and edges instead of a freeform JSON definition.
                  </p>
                </div>
                {selectedTemplateRecord ? (
                  <Badge variant={selectedTemplateRecord.published_at ? "default" : "outline"}>
                    {selectedTemplateRecord.published_at ? `Published v${selectedTemplateRecord.definition_version}` : "Draft"}
                  </Badge>
                ) : null}
              </div>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="grid gap-4 md:grid-cols-2">
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Team key</label>
                  <Input value={teamDraft.team_key} disabled={Boolean(selectedTemplateId)} onChange={(event) => setTeamDraft((current) => ({ ...current, team_key: event.target.value }))} />
                </div>
                <div className="space-y-2">
                  <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Label</label>
                  <Input value={teamDraft.label} onChange={(event) => setTeamDraft((current) => ({ ...current, label: event.target.value }))} />
                </div>
              </div>
              <div className="space-y-2">
                <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Description</label>
                <Textarea value={teamDraft.description} onChange={(event) => setTeamDraft((current) => ({ ...current, description: event.target.value }))} className="min-h-[88px]" />
              </div>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={teamDraft.is_active} onChange={(event) => setTeamDraft((current) => ({ ...current, is_active: event.target.checked }))} />
                Active
              </label>

              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold">Roles</h3>
                  <Button variant="outline" size="sm" onClick={() => setTeamDraft((current) => ({ ...current, roles: [...current.roles, emptyRoleDraft()] }))}>
                    Add role
                  </Button>
                </div>
                {teamDraft.roles.map((role, index) => (
                  <div key={`role-${index}`} className="grid gap-3 rounded-lg border p-3 md:grid-cols-6">
                    <Input placeholder="role_key" value={role.role_key} onChange={(event) => setTeamDraft((current) => ({ ...current, roles: current.roles.map((entry, entryIndex) => entryIndex === index ? { ...entry, role_key: event.target.value } : entry) }))} />
                    <Input placeholder="Label" value={role.label} onChange={(event) => setTeamDraft((current) => ({ ...current, roles: current.roles.map((entry, entryIndex) => entryIndex === index ? { ...entry, label: event.target.value } : entry) }))} />
                    <Input placeholder="Description" value={role.description} onChange={(event) => setTeamDraft((current) => ({ ...current, roles: current.roles.map((entry, entryIndex) => entryIndex === index ? { ...entry, description: event.target.value } : entry) }))} />
                    <Input type="number" placeholder="Position" value={String(role.position)} onChange={(event) => setTeamDraft((current) => ({ ...current, roles: current.roles.map((entry, entryIndex) => entryIndex === index ? { ...entry, position: Number(event.target.value) || 1 } : entry) }))} />
                    <select className="h-10 rounded-md border border-input bg-background px-3 text-sm" value={role.persona_key} onChange={(event) => setTeamDraft((current) => ({ ...current, roles: current.roles.map((entry, entryIndex) => entryIndex === index ? { ...entry, persona_key: event.target.value } : entry) }))}>
                      <option value="">Persona</option>
                      {personaOptions.map((persona) => (
                        <option key={persona.persona_id} value={persona.persona_key}>
                          {persona.label}
                        </option>
                      ))}
                    </select>
                    <select className="h-10 rounded-md border border-input bg-background px-3 text-sm" value={role.agent_key} onChange={(event) => setTeamDraft((current) => ({ ...current, roles: current.roles.map((entry, entryIndex) => entryIndex === index ? { ...entry, agent_key: event.target.value } : entry) }))}>
                      <option value="">Agent</option>
                      {agentOptions.map((agent) => (
                        <option key={agent.agent_id} value={agent.agent_key}>
                          {agent.label}
                        </option>
                      ))}
                    </select>
                  </div>
                ))}
              </div>

              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold">Tasks</h3>
                  <Button variant="outline" size="sm" onClick={() => setTeamDraft((current) => ({ ...current, tasks: [...current.tasks, emptyTaskDraft(current.tasks.length + 1)] }))}>
                    Add task
                  </Button>
                </div>
                {teamDraft.tasks.map((task, index) => (
                  <div key={`task-${index}`} className="grid gap-3 rounded-lg border p-3 md:grid-cols-6">
                    <Input placeholder="task_key" value={task.task_key} onChange={(event) => setTeamDraft((current) => ({ ...current, tasks: current.tasks.map((entry, entryIndex) => entryIndex === index ? { ...entry, task_key: event.target.value } : entry) }))} />
                    <Input placeholder="Label" value={task.label} onChange={(event) => setTeamDraft((current) => ({ ...current, tasks: current.tasks.map((entry, entryIndex) => entryIndex === index ? { ...entry, label: event.target.value } : entry) }))} />
                    <select className="h-10 rounded-md border border-input bg-background px-3 text-sm" value={task.owner_role_key} onChange={(event) => setTeamDraft((current) => ({ ...current, tasks: current.tasks.map((entry, entryIndex) => entryIndex === index ? { ...entry, owner_role_key: event.target.value } : entry) }))}>
                      <option value="">Owner role</option>
                      {teamDraft.roles.map((role) => (
                        <option key={`owner-role-${index}-${role.role_key || "draft"}`} value={role.role_key}>
                          {role.label || role.role_key || "Unnamed role"}
                        </option>
                      ))}
                    </select>
                    <Input type="number" placeholder="Position" value={String(task.position)} onChange={(event) => setTeamDraft((current) => ({ ...current, tasks: current.tasks.map((entry, entryIndex) => entryIndex === index ? { ...entry, position: Number(event.target.value) || 1 } : entry) }))} />
                    <Input placeholder="Produces (csv)" value={task.produces} onChange={(event) => setTeamDraft((current) => ({ ...current, tasks: current.tasks.map((entry, entryIndex) => entryIndex === index ? { ...entry, produces: event.target.value } : entry) }))} />
                    <Input placeholder="Consumes (csv)" value={task.consumes} onChange={(event) => setTeamDraft((current) => ({ ...current, tasks: current.tasks.map((entry, entryIndex) => entryIndex === index ? { ...entry, consumes: event.target.value } : entry) }))} />
                    <Input placeholder="Approval type" value={task.approval_type} onChange={(event) => setTeamDraft((current) => ({ ...current, tasks: current.tasks.map((entry, entryIndex) => entryIndex === index ? { ...entry, approval_type: event.target.value } : entry) }))} />
                  </div>
                ))}
              </div>

              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold">Edges</h3>
                  <Button variant="outline" size="sm" onClick={() => setTeamDraft((current) => ({ ...current, edges: [...current.edges, { from_task_key: "", to_task_key: "" }] }))}>
                    Add edge
                  </Button>
                </div>
                {teamDraft.edges.map((edge, index) => (
                  <div key={`edge-${index}`} className="grid gap-3 rounded-lg border p-3 md:grid-cols-2">
                    <select className="h-10 rounded-md border border-input bg-background px-3 text-sm" value={edge.from_task_key} onChange={(event) => setTeamDraft((current) => ({ ...current, edges: current.edges.map((entry, entryIndex) => entryIndex === index ? { ...entry, from_task_key: event.target.value } : entry) }))}>
                      <option value="">From task</option>
                      {teamDraft.tasks.map((task) => (
                        <option key={`from-${task.task_key || index}`} value={task.task_key}>
                          {task.label || task.task_key || "Unnamed task"}
                        </option>
                      ))}
                    </select>
                    <select className="h-10 rounded-md border border-input bg-background px-3 text-sm" value={edge.to_task_key} onChange={(event) => setTeamDraft((current) => ({ ...current, edges: current.edges.map((entry, entryIndex) => entryIndex === index ? { ...entry, to_task_key: event.target.value } : entry) }))}>
                      <option value="">To task</option>
                      {teamDraft.tasks.map((task) => (
                        <option key={`to-${task.task_key || index}`} value={task.task_key}>
                          {task.label || task.task_key || "Unnamed task"}
                        </option>
                      ))}
                    </select>
                  </div>
                ))}
              </div>

              <div className="flex justify-end gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setSelectedTemplateId(null);
                    setTeamDraft(emptyTeamDraft());
                  }}
                >
                  Reset
                </Button>
                <Button size="sm" onClick={() => void saveTeamTemplate()} disabled={saving || !teamDraft.team_key.trim() || !teamDraft.label.trim()}>
                  <Save className="mr-1.5 h-3.5 w-3.5" />
                  Save template
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      ) : null}

      {activeTab === "runtime" ? (
        <div className="grid gap-6 md:grid-cols-3">
          {[
            {
              title: "Roles",
              description: "Runtime role keys derived from persisted agents and templates.",
              items: runtimeBindings?.available_roles ?? [],
            },
            {
              title: "Named agents",
              description: "Named-agent routing keys exposed to runtime policy.",
              items: runtimeBindings?.available_named_agents ?? [],
            },
            {
              title: "Selectors",
              description: "Selector-driven routing keys available to platform runtime configuration.",
              items: runtimeBindings?.available_selectors ?? [],
            },
          ].map((section) => (
            <Card key={section.title}>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">{section.title}</CardTitle>
                <p className="text-sm text-muted-foreground">{section.description}</p>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                {section.items.length === 0 ? (
                  <p className="text-muted-foreground">No bindings published yet.</p>
                ) : (
                  section.items.map((item) => (
                    <div key={item} className="rounded-md border bg-muted/20 px-3 py-2 font-mono text-xs">
                      {item}
                    </div>
                  ))
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      ) : null}
    </div>
  );
}
