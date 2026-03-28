"use client";

import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  createTenantInvite,
  createTenantTeam,
  listTenantInvites,
  listTenantMembers,
  listTenantTeams,
  revokeTenantInvite,
  resendTenantInvite,
  updateTenantMemberRecord,
  updateTenantTeamRecord,
  type TenantInviteRecord,
  type TenantMemberRecord,
  type TenantTeamRecord,
} from "@/lib/api";

export type TenantTeamSection = "members" | "teams" | "invites";

const TEAM_PERMISSION_OPTIONS = [
  { key: "workspace.manage", label: "Manage workspace" },
  { key: "people.manage", label: "Manage people" },
  { key: "projects.manage", label: "Manage projects" },
  { key: "technical.access", label: "Technical access" },
] as const;

const ROLE_OPTIONS = [
  { value: "business_member", label: "Business member" },
  { value: "technical_member", label: "Technical member" },
  { value: "tenant_admin", label: "Tenant admin" },
] as const;

function labelForRole(role: TenantMemberRecord["role"] | TenantInviteRecord["role"]): string {
  return ROLE_OPTIONS.find((option) => option.value === role)?.label ?? role;
}

function labelForInviteStatus(status: TenantInviteRecord["status"]): string {
  return (
    {
      pending: "Pending",
      accepted: "Accepted",
      revoked: "Revoked",
      expired: "Expired",
    }[status] ?? status
  );
}

function describeDiscordState(member: TenantMemberRecord): string {
  const items = [
    member.discord_state.linked ? "Linked" : "Not linked",
    member.discord_state.guild_joined ? "Joined server" : "Not joined",
    member.discord_state.welcome_status === "sent"
      ? "Welcome sent"
      : member.discord_state.welcome_status === "failed"
        ? "Welcome failed"
        : null,
  ].filter(Boolean);
  return items.join(" • ");
}

function summarizePermissions(permissionKeys: string[]): string {
  const labels = TEAM_PERMISSION_OPTIONS.filter((option) => permissionKeys.includes(option.key)).map((option) => option.label);
  if (labels.length === 0) {
    return "Standard workspace access";
  }
  if (labels.length <= 3) {
    return labels.join(", ");
  }
  return `${labels.slice(0, 3).join(", ")} + ${labels.length - 3} more`;
}

export function TenantTeamPage({ section }: { section: TenantTeamSection }) {
  const params = useParams<{ tenantId: string }>();
  const { credentials, ready } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);

  const [members, setMembers] = useState<TenantMemberRecord[]>([]);
  const [teams, setTeams] = useState<TenantTeamRecord[]>([]);
  const [invites, setInvites] = useState<TenantInviteRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteName, setInviteName] = useState("");
  const [inviteRole, setInviteRole] = useState<"tenant_admin" | "technical_member" | "business_member">("business_member");
  const [inviteTeamIds, setInviteTeamIds] = useState<string[]>([]);
  const [newTeamName, setNewTeamName] = useState("");
  const [newTeamDescription, setNewTeamDescription] = useState("");
  const [newTeamPermissions, setNewTeamPermissions] = useState<string[]>([]);
  const [showCreateTeamForm, setShowCreateTeamForm] = useState(false);
  const [expandedTeamId, setExpandedTeamId] = useState<string | null>(null);

  const statusClasses = useMemo(() => {
    const normalized = statusLine.toLowerCase();
    if (normalized.includes("failed") || normalized.includes("unable") || normalized.includes("error")) {
      return "border-red-300 bg-red-50 text-red-800";
    }
    return "border-muted bg-muted/30 text-muted-foreground";
  }, [statusLine]);

  const teamNameById = useMemo(() => new Map(teams.map((team) => [team.team_id, team.name])), [teams]);

  async function loadTeamData() {
    if (!credentials) {
      return;
    }
    setLoading(true);
    try {
      const [nextMembers, nextTeams, nextInvites] = await Promise.all([
        listTenantMembers(credentials, tenantId),
        listTenantTeams(credentials, tenantId),
        listTenantInvites(credentials, tenantId),
      ]);
      setMembers(nextMembers);
      setTeams(nextTeams);
      setInvites(nextInvites.items);
    } catch (error) {
      setStatusLine(`Failed to load team settings: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) {
      void loadTeamData();
    }
  }, [ready, credentials, tenantId]);

  async function handleCreateInvite() {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      await createTenantInvite(credentials, tenantId, {
        email: inviteEmail.trim(),
        full_name: inviteName.trim() || null,
        role: inviteRole,
        team_ids: inviteTeamIds,
        mode_override: null,
      });
      setInviteEmail("");
      setInviteName("");
      setInviteTeamIds([]);
      await loadTeamData();
      setStatusLine("Invite created.");
    } catch (error) {
      setStatusLine(`Unable to create invite: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleCreateTeam() {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      await createTenantTeam(credentials, tenantId, {
        name: newTeamName.trim(),
        description: newTeamDescription.trim() || null,
        permission_keys: newTeamPermissions,
      });
      setNewTeamName("");
      setNewTeamDescription("");
      setNewTeamPermissions([]);
      setShowCreateTeamForm(false);
      await loadTeamData();
      setStatusLine("Team created.");
    } catch (error) {
      setStatusLine(`Unable to create team: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleToggleTeamPermission(team: TenantTeamRecord, permissionKey: string) {
    if (!credentials) {
      return;
    }
    const nextPermissions = team.permission_keys.includes(permissionKey)
      ? team.permission_keys.filter((key) => key !== permissionKey)
      : [...team.permission_keys, permissionKey];
    setBusy(true);
    try {
      await updateTenantTeamRecord(credentials, tenantId, team.team_id, {
        name: team.name,
        description: team.description,
        permission_keys: nextPermissions,
      });
      await loadTeamData();
      setStatusLine(`Updated team ${team.name}.`);
    } catch (error) {
      setStatusLine(`Unable to update team: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleUpdateMember(
    member: TenantMemberRecord,
    patch: Partial<Pick<TenantMemberRecord, "role" | "is_active" | "team_ids">>,
  ) {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      await updateTenantMemberRecord(credentials, tenantId, member.membership_id, {
        role: (patch.role ?? member.role) as "tenant_admin" | "technical_member" | "business_member",
        team_ids: patch.team_ids ?? member.team_ids,
        mode_override: member.mode_override,
        is_active: patch.is_active ?? member.is_active,
      });
      await loadTeamData();
      setStatusLine(`Updated member ${member.email}.`);
    } catch (error) {
      setStatusLine(`Unable to update member: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleInviteAction(inviteId: string, action: "resend" | "revoke") {
    if (!credentials) {
      return;
    }
    setBusy(true);
    try {
      if (action === "resend") {
        await resendTenantInvite(credentials, tenantId, inviteId);
      } else {
        await revokeTenantInvite(credentials, tenantId, inviteId);
      }
      await loadTeamData();
      setStatusLine(`Invite ${action} complete.`);
    } catch (error) {
      setStatusLine(`Unable to ${action} invite: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  if (loading) {
    return (
      <Card>
        <CardHeader>
          <Skeleton className="h-6 w-64" />
          <Skeleton className="h-4 w-80" />
        </CardHeader>
        <CardContent className="space-y-3">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-32 w-full rounded-md border" />
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {statusLine ? <div className={`rounded-md border px-3 py-2 text-sm ${statusClasses}`}>{statusLine}</div> : null}

      {section === "members" ? (
        <Card>
          <CardHeader>
            <CardTitle>Members</CardTitle>
            <CardDescription>Manage people, role, team membership, and onboarding status.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            {members.length === 0 ? (
              <p className="text-muted-foreground">No members found.</p>
            ) : (
              <div className="space-y-3">
                {members.map((member) => (
                  <div key={member.membership_id} className="rounded-lg border p-3">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <p className="font-medium">{member.full_name || member.email}</p>
                        <p className="text-xs text-muted-foreground">{member.email}</p>
                        <p className="text-xs text-muted-foreground">Discord: {describeDiscordState(member)}</p>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <select
                          className="rounded-md border bg-background px-2 py-1"
                          value={member.role}
                          onChange={(event) =>
                            void handleUpdateMember(member, {
                              role: event.target.value as TenantMemberRecord["role"],
                            })
                          }
                        >
                          {ROLE_OPTIONS.map((option) => (
                            <option key={option.value} value={option.value}>
                              {option.label}
                            </option>
                          ))}
                        </select>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => void handleUpdateMember(member, { is_active: !member.is_active })}
                        >
                          {member.is_active ? "Deactivate" : "Reactivate"}
                        </Button>
                      </div>
                    </div>
                    <div className="mt-3 flex flex-wrap gap-2">
                      {teams.map((team) => {
                        const assigned = member.team_ids.includes(team.team_id);
                        return (
                          <Button
                            key={`${member.membership_id}-${team.team_id}`}
                            variant={assigned ? "default" : "outline"}
                            size="sm"
                            onClick={() =>
                              void handleUpdateMember(member, {
                                team_ids: assigned
                                  ? member.team_ids.filter((teamId) => teamId !== team.team_id)
                                  : [...member.team_ids, team.team_id],
                              })
                            }
                          >
                            {team.name}
                          </Button>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      ) : null}

      {section === "teams" ? (
        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="space-y-1">
                <CardTitle>Teams</CardTitle>
                <CardDescription>Create groups and decide what each team can access.</CardDescription>
              </div>
              <Button type="button" variant={showCreateTeamForm ? "outline" : "default"} onClick={() => setShowCreateTeamForm((current) => !current)}>
                {showCreateTeamForm ? "Close" : "New team"}
              </Button>
            </div>
          </CardHeader>
          <CardContent className="space-y-4 text-sm">
            {showCreateTeamForm ? (
              <div className="grid gap-3 rounded-lg border p-3 md:grid-cols-2">
                <Input placeholder="Team name" value={newTeamName} onChange={(event) => setNewTeamName(event.target.value)} />
                <Input
                  placeholder="Description"
                  value={newTeamDescription}
                  onChange={(event) => setNewTeamDescription(event.target.value)}
                />
                <div className="space-y-2 md:col-span-2">
                  <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Access</p>
                  <div className="flex flex-wrap gap-2">
                    {TEAM_PERMISSION_OPTIONS.map((permissionOption) => {
                      const selected = newTeamPermissions.includes(permissionOption.key);
                      return (
                        <Button
                          key={permissionOption.key}
                          type="button"
                          variant={selected ? "default" : "outline"}
                          size="sm"
                          onClick={() =>
                            setNewTeamPermissions((current) =>
                              current.includes(permissionOption.key)
                                ? current.filter((item) => item !== permissionOption.key)
                                : [...current, permissionOption.key],
                            )
                          }
                        >
                          {permissionOption.label}
                        </Button>
                      );
                    })}
                  </div>
                </div>
                <div className="flex flex-wrap gap-2 md:col-span-2">
                  <Button onClick={() => void handleCreateTeam()} disabled={busy || !newTeamName.trim()}>
                    Create team
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => {
                      setShowCreateTeamForm(false);
                      setNewTeamName("");
                      setNewTeamDescription("");
                      setNewTeamPermissions([]);
                    }}
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            ) : null}

            <div className="space-y-3">
              {teams.length === 0 ? <p className="text-muted-foreground">No teams created yet.</p> : null}
              {teams.map((team) => (
                <div key={team.team_id} className="rounded-lg border p-3">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <p className="font-medium">{team.name}</p>
                      <p className="text-xs text-muted-foreground">{team.description || "No description"}</p>
                      <p className="mt-2 text-xs text-muted-foreground">Access: {summarizePermissions(team.permission_keys)}</p>
                    </div>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => setExpandedTeamId((current) => (current === team.team_id ? null : team.team_id))}
                    >
                      {expandedTeamId === team.team_id ? "Done" : "Edit access"}
                    </Button>
                  </div>
                  {expandedTeamId === team.team_id ? (
                    <div className="mt-3 flex flex-wrap gap-2">
                      {TEAM_PERMISSION_OPTIONS.map((permissionOption) => (
                        <Button
                          key={`${team.team_id}-${permissionOption.key}`}
                          variant={team.permission_keys.includes(permissionOption.key) ? "default" : "outline"}
                          size="sm"
                          onClick={() => void handleToggleTeamPermission(team, permissionOption.key)}
                        >
                          {permissionOption.label}
                        </Button>
                      ))}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      ) : null}

      {section === "invites" ? (
        <Card>
          <CardHeader>
            <CardTitle>Invites</CardTitle>
            <CardDescription>Invite people to the workspace and choose their access.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4 text-sm">
            <div className="grid gap-3 rounded-lg border p-3 md:grid-cols-2">
              <Input placeholder="Email" value={inviteEmail} onChange={(event) => setInviteEmail(event.target.value)} />
              <Input placeholder="Full name" value={inviteName} onChange={(event) => setInviteName(event.target.value)} />
              <select
                aria-label="Role"
                className="rounded-md border bg-background px-3 py-2"
                value={inviteRole}
                onChange={(event) =>
                  setInviteRole(event.target.value as "tenant_admin" | "technical_member" | "business_member")
                }
              >
                {ROLE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              <div className="space-y-2 md:col-span-2">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Assign teams</p>
                <div className="flex flex-wrap gap-2 rounded-md border p-3">
                  {teams.length === 0 ? (
                    <p className="text-xs text-muted-foreground">Create a team first, then assign it here.</p>
                  ) : (
                    teams.map((team) => {
                      const selected = inviteTeamIds.includes(team.team_id);
                      return (
                        <Button
                          key={`invite-team-${team.team_id}`}
                          type="button"
                          variant={selected ? "default" : "outline"}
                          size="sm"
                          onClick={() =>
                            setInviteTeamIds((current) =>
                              current.includes(team.team_id)
                                ? current.filter((teamId) => teamId !== team.team_id)
                                : [...current, team.team_id],
                            )
                          }
                        >
                          {team.name}
                        </Button>
                      );
                    })
                  )}
                </div>
              </div>
              <div className="md:col-span-2">
                <Button onClick={() => void handleCreateInvite()} disabled={busy || !inviteEmail.trim()}>
                  Send invite
                </Button>
              </div>
            </div>

            <div className="space-y-3">
              {invites.map((invite) => (
                <div key={invite.invite_id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3">
                  <div>
                    <p className="font-medium">{invite.email}</p>
                    <p className="text-xs text-muted-foreground">
                      {labelForInviteStatus(invite.status)} • {labelForRole(invite.role)} •{" "}
                      {invite.team_ids.length > 0
                        ? invite.team_ids.map((teamId) => teamNameById.get(teamId) ?? "Unknown team").join(", ")
                        : "No teams assigned"}
                    </p>
                  </div>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" onClick={() => void handleInviteAction(invite.invite_id, "resend")}>
                      Resend
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => void handleInviteAction(invite.invite_id, "revoke")}>
                      Revoke
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
