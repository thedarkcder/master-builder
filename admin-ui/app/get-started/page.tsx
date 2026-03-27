"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { CheckCircle2, Circle, Disc3, Github, KanbanSquare, Users } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  completeOnboarding,
  createTenantDiscordInvite,
  getTenant,
  getTenantDiscordIdentity,
  startTenantDiscordLink,
  type TenantDiscordIdentityRecord,
  type TenantRecord
} from "@/lib/api";

function ChecklistRow({ done, label }: { done: boolean; label: string }) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border/60 bg-background/70 px-4 py-3">
      {done ? <CheckCircle2 className="h-4 w-4 text-emerald-500" /> : <Circle className="h-4 w-4 text-muted-foreground" />}
      <span className="text-sm">{label}</span>
    </div>
  );
}

export default function GetStartedPage() {
  const router = useRouter();
  const { credentials, principal, ready, needsOnboarding, refreshPrincipal } = useAuth();
  const [tenant, setTenant] = useState<TenantRecord | null>(null);
  const [loadingTenant, setLoadingTenant] = useState(false);
  const [isCompleting, setIsCompleting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [discordIdentity, setDiscordIdentity] = useState<TenantDiscordIdentityRecord | null>(null);
  const [discordBusy, setDiscordBusy] = useState(false);

  const pendingMembership = useMemo(
    () => principal?.memberships.find((membership) => membership.onboarding_completed_at == null) ?? null,
    [principal]
  );

  useEffect(() => {
    if (ready && !credentials) {
      router.replace("/login");
    }
  }, [credentials, ready, router]);

  useEffect(() => {
    if (!credentials || !pendingMembership) {
      setTenant(null);
      setDiscordIdentity(null);
      return;
    }
    let cancelled = false;
    setLoadingTenant(true);
    void Promise.all([
      getTenant(credentials, pendingMembership.tenant_id),
      getTenantDiscordIdentity(credentials, pendingMembership.tenant_id)
    ])
      .then(([record, identity]) => {
        if (!cancelled) {
          setTenant(record);
          setDiscordIdentity(identity);
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setErrorMessage(`Failed to load onboarding context: ${(error as Error).message}`);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoadingTenant(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [credentials, pendingMembership]);

  useEffect(() => {
    if (ready && credentials && !needsOnboarding) {
      router.replace("/dashboard");
    }
  }, [credentials, needsOnboarding, ready, router]);

  if (!ready || !credentials || !principal || !pendingMembership) {
    return <main className="p-8 text-sm text-muted-foreground">Loading onboarding…</main>;
  }

  const isTenantAdminSetup = pendingMembership.onboarding_kind === "tenant_admin_setup";
  const discordReady = Boolean(tenant?.discord?.guild_id && tenant?.discord?.onboarding_channel_id);
  const jiraReady = Boolean(tenant?.jira.connection_id);
  const githubReady = Boolean(tenant?.github.installation_id);
  const canCompleteAdminSetup = Boolean(jiraReady && githubReady && discordReady);
  const memberDiscordLinked = Boolean(discordIdentity?.linked || pendingMembership.discord_state?.linked);
  const memberDiscordJoined = Boolean(pendingMembership.discord_state?.guild_joined);

  async function handleComplete() {
    if (!credentials || !pendingMembership) {
      return;
    }
    setErrorMessage(null);
    setIsCompleting(true);
    try {
      await completeOnboarding(credentials, pendingMembership.tenant_id);
      await refreshPrincipal();
      router.replace("/dashboard");
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Unable to complete onboarding");
    } finally {
      setIsCompleting(false);
    }
  }

  async function handleLinkDiscord() {
    if (!credentials || !pendingMembership) {
      return;
    }
    setDiscordBusy(true);
    try {
      const result = await startTenantDiscordLink(credentials, pendingMembership.tenant_id, "/get-started");
      window.location.href = result.authorize_url;
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Unable to start Discord link");
      setDiscordBusy(false);
    }
  }

  async function handleCreateDiscordInvite() {
    if (!credentials || !pendingMembership) {
      return;
    }
    setDiscordBusy(true);
    try {
      const invite = await createTenantDiscordInvite(credentials, pendingMembership.tenant_id);
      window.open(invite.invite_url, "_blank", "noopener,noreferrer");
      await refreshPrincipal();
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Unable to create Discord invite");
    } finally {
      setDiscordBusy(false);
    }
  }

  return (
    <main className="min-h-screen bg-[linear-gradient(180deg,#f8fafc_0%,#eef2ff_45%,#fff7ed_100%)] px-4 py-10">
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-6">
        <div className="max-w-2xl space-y-3">
          <span className="inline-flex items-center rounded-full border border-slate-300 bg-white/70 px-3 py-1 text-xs font-medium uppercase tracking-[0.18em] text-slate-600">
            Get Started
          </span>
          <h1 className="text-3xl font-semibold tracking-tight text-slate-900">
            {isTenantAdminSetup ? "Set up your workspace before the team starts shipping." : "Finish your team onboarding."}
          </h1>
          <p className="text-sm leading-6 text-slate-600">
            {isTenantAdminSetup
              ? "Connect Jira, GitHub, and Discord so Master Builder can organize work, code, and team communication from the first day."
              : "You’ve been added to a Master Builder tenant. Review your role, join the team space, and then continue into delivery tracking."}
          </p>
        </div>

        {errorMessage ? (
          <p className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{errorMessage}</p>
        ) : null}

        <div className="grid gap-6 lg:grid-cols-[1.25fr_0.85fr]">
          <Card className="border-slate-200/80 bg-white/85 shadow-sm">
            <CardHeader>
              <CardTitle>{tenant?.name ?? pendingMembership.tenant_id}</CardTitle>
              <CardDescription>
                {isTenantAdminSetup
                  ? "This checklist unlocks the product for your tenant."
                  : "This is your entry point into the tenant workspace."}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              {isTenantAdminSetup ? (
                <>
                  <ChecklistRow done={jiraReady} label="Connect Jira and choose at least one project" />
                  <ChecklistRow done={githubReady} label="Install the GitHub App for this tenant" />
                  <ChecklistRow done={discordReady} label="Install the Discord bot and choose an onboarding channel" />
                  <div className="flex flex-wrap gap-3 pt-2">
                    <Button asChild>
                      <Link href={`/tenants/new/basics?tenant_id=${encodeURIComponent(pendingMembership.tenant_id)}`}>
                        Continue setup wizard
                      </Link>
                    </Button>
                    <Button asChild variant="outline">
                      <Link href={`/tenants/new/discord?tenant_id=${encodeURIComponent(pendingMembership.tenant_id)}`}>
                        Open Discord Setup
                      </Link>
                    </Button>
                    <Button variant="outline" onClick={() => void handleComplete()} disabled={!canCompleteAdminSetup || isCompleting || loadingTenant}>
                      {isCompleting ? "Finishing..." : "Finish setup"}
                    </Button>
                  </div>
                </>
              ) : (
                <>
                  <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-4">
                    <p className="text-sm font-medium text-slate-900">Role</p>
                    <p className="mt-1 text-sm text-slate-600">{pendingMembership.role.replace(/_/g, " ")}</p>
                  </div>
                  <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-4">
                    <p className="text-sm font-medium text-slate-900">Experience mode</p>
                    <p className="mt-1 text-sm text-slate-600">{pendingMembership.effective_mode.replace(/_/g, " ")}</p>
                  </div>
                  <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-4">
                    <p className="text-sm font-medium text-slate-900">Teams</p>
                    <p className="mt-1 text-sm text-slate-600">
                      {pendingMembership.team_ids.length > 0 ? pendingMembership.team_ids.join(", ") : "No team assignments yet"}
                    </p>
                  </div>
                  <ChecklistRow done={memberDiscordLinked} label="Link your Discord identity" />
                  <ChecklistRow done={memberDiscordJoined} label="Join the tenant Discord server" />
                  <div className="flex flex-wrap gap-3 pt-2">
                    <Button variant="outline" onClick={() => void handleLinkDiscord()} disabled={discordBusy || memberDiscordLinked}>
                      {memberDiscordLinked ? "Discord linked" : "Link Discord"}
                    </Button>
                    <Button variant="outline" onClick={() => void handleCreateDiscordInvite()} disabled={discordBusy || !memberDiscordLinked}>
                      Join Discord
                    </Button>
                    <Button onClick={() => void handleComplete()} disabled={isCompleting}>
                      {isCompleting ? "Continuing..." : "Continue to workspace"}
                    </Button>
                  </div>
                </>
              )}
            </CardContent>
          </Card>

          <Card className="border-slate-200/80 bg-slate-950 text-slate-50 shadow-sm">
            <CardHeader>
              <CardTitle>What happens next</CardTitle>
              <CardDescription className="text-slate-300">
                {isTenantAdminSetup
                  ? "These are the integrations the product expects before normal access."
                  : "The team onboarding flow is shorter for invited members."}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex items-start gap-3">
                <KanbanSquare className="mt-0.5 h-4 w-4 text-amber-300" />
                <div>
                  <p className="text-sm font-medium">Jira</p>
                  <p className="text-sm text-slate-300">Connect issue intake so delivery updates can roll into business analytics.</p>
                </div>
              </div>
              <div className="flex items-start gap-3">
                <Github className="mt-0.5 h-4 w-4 text-sky-300" />
                <div>
                  <p className="text-sm font-medium">GitHub</p>
                  <p className="text-sm text-slate-300">Install the app and choose repositories the tenant can operate against.</p>
                </div>
              </div>
              <div className="flex items-start gap-3">
                <Disc3 className="mt-0.5 h-4 w-4 text-emerald-300" />
                <div>
                  <p className="text-sm font-medium">Discord</p>
                  <p className="text-sm text-slate-300">Configure the team channel that onboarding invites and member welcomes should target.</p>
                </div>
              </div>
              <div className="flex items-start gap-3">
                <Users className="mt-0.5 h-4 w-4 text-rose-300" />
                <div>
                  <p className="text-sm font-medium">Teams and permissions</p>
                  <p className="text-sm text-slate-300">After setup, admins can invite people by email and tune business versus technical views.</p>
                </div>
              </div>
            </CardContent>
          </Card>
        </div>
      </div>
    </main>
  );
}
