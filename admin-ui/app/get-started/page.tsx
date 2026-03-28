"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { CheckCircle2, Circle } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { DiscordLogo } from "@/components/icons/discord-logo";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { getDefaultAuthenticatedRoute, getTenantDashboardRoute } from "@/lib/auth-routing";
import {
  completeOnboarding,
  createTenantDiscordInvite,
  getTenant,
  getTenantDiscordIdentity,
  startTenantDiscordLink,
  updateTenantUserSettings,
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

type MemberOnboardingStep = "details" | "experience" | "discord" | "finish";

const MEMBER_ONBOARDING_STEPS: Array<{ key: MemberOnboardingStep; label: string }> = [
  { key: "details", label: "Confirm details" },
  { key: "experience", label: "Select experience" },
  { key: "discord", label: "Join Discord" },
  { key: "finish", label: "Finish" },
];

export default function GetStartedPage() {
  const router = useRouter();
  const { credentials, principal, ready, needsOnboarding, refreshPrincipal } = useAuth();
  const [tenant, setTenant] = useState<TenantRecord | null>(null);
  const [loadingTenant, setLoadingTenant] = useState(false);
  const [isCompleting, setIsCompleting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [discordIdentity, setDiscordIdentity] = useState<TenantDiscordIdentityRecord | null>(null);
  const [discordBusy, setDiscordBusy] = useState(false);
  const [memberStep, setMemberStep] = useState<MemberOnboardingStep>("details");
  const [selectedExperience, setSelectedExperience] = useState<"technical" | "non_technical">("non_technical");
  const [experienceBusy, setExperienceBusy] = useState(false);

  const pendingMembership = useMemo(
    () => principal?.memberships.find((membership) => membership.onboarding_completed_at == null) ?? null,
    [principal]
  );

  const isTenantAdminSetup = pendingMembership?.onboarding_kind === "tenant_admin_setup";

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
    if (ready && credentials && principal && !needsOnboarding) {
      router.replace(getDefaultAuthenticatedRoute(principal));
    }
  }, [credentials, needsOnboarding, principal, ready, router]);

  useEffect(() => {
    if (!pendingMembership || isTenantAdminSetup) {
      return;
    }
    setSelectedExperience(pendingMembership.effective_mode === "technical" ? "technical" : "non_technical");
  }, [isTenantAdminSetup, pendingMembership]);

  useEffect(() => {
    if (!pendingMembership || isTenantAdminSetup) {
      return;
    }
    setMemberStep("details");
  }, [isTenantAdminSetup, pendingMembership?.membership_id]);
  const discordReady = Boolean(tenant?.discord?.guild_id && tenant?.discord?.onboarding_channel_id);
  const jiraReady = Boolean(tenant?.jira.connection_id);
  const githubReady = Boolean(tenant?.github.installation_id);
  const memberSteps = useMemo(
    () =>
      discordReady
        ? MEMBER_ONBOARDING_STEPS
        : MEMBER_ONBOARDING_STEPS.filter((step) => step.key !== "discord"),
    [discordReady]
  );

  useEffect(() => {
    if (memberStep === "discord" && !discordReady) {
      setMemberStep("finish");
    }
  }, [discordReady, memberStep]);

  if (!ready || !credentials || !principal || !pendingMembership) {
    return <main className="p-8 text-sm text-muted-foreground">Loading onboarding…</main>;
  }

  const canCompleteAdminSetup = Boolean(jiraReady && githubReady && discordReady);
  const memberDiscordLinked = Boolean(discordIdentity?.linked || pendingMembership.discord_state?.linked);
  const memberDiscordJoined = Boolean(pendingMembership.discord_state?.guild_joined);
  const canChooseTechnicalExperience =
    pendingMembership.permission_keys.includes("analytics.technical.view") ||
    pendingMembership.permission_keys.includes("runs.technical.view") ||
    pendingMembership.permission_keys.includes("settings.technical.view");
  const activeStepIndex = memberSteps.findIndex((step) => step.key === memberStep);
  const teamList = pendingMembership.team_ids.length > 0 ? pendingMembership.team_ids.join(", ") : "Not assigned";
  const discordAction =
    !discordReady ? null : !memberDiscordLinked
      ? {
          label: "Link Discord",
          disabled: discordBusy,
          onClick: () => void handleLinkDiscord(),
        }
      : !memberDiscordJoined
        ? {
            label: "Open Discord invite",
            disabled: discordBusy,
            onClick: () => void handleCreateDiscordInvite(),
          }
        : null;

  async function handleComplete() {
    if (!credentials || !pendingMembership) {
      return;
    }
    setErrorMessage(null);
    setIsCompleting(true);
    try {
      await completeOnboarding(credentials, pendingMembership.tenant_id);
      await refreshPrincipal();
      router.replace(getTenantDashboardRoute(pendingMembership.tenant_id));
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

  async function handleContinueFromExperience() {
    if (!credentials || !pendingMembership) {
      return;
    }
    setExperienceBusy(true);
    setErrorMessage(null);
    try {
      await updateTenantUserSettings(credentials, pendingMembership.tenant_id, {
        mode_override: selectedExperience,
      });
      await refreshPrincipal();
      setMemberStep(discordReady ? "discord" : "finish");
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Unable to save experience");
    } finally {
      setExperienceBusy(false);
    }
  }

  return (
    <main className="min-h-screen bg-slate-50 px-4 py-10">
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-6">
        <div className="max-w-2xl space-y-3">
          <h1 className="text-3xl font-semibold tracking-tight text-slate-900">
            {isTenantAdminSetup ? "Set up workspace" : "Join workspace"}
          </h1>
          <p className="text-sm text-slate-600">
            {isTenantAdminSetup
              ? "Connect the required systems before your team starts using the workspace."
              : "Complete the steps to enter the workspace."}
          </p>
        </div>

        {errorMessage ? (
          <p className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{errorMessage}</p>
        ) : null}

        <div className="grid gap-6">
          <Card className="border-slate-200/80 bg-white/85 shadow-sm">
              <CardHeader>
              <CardTitle>{tenant?.name ?? pendingMembership.tenant_id}</CardTitle>
              {isTenantAdminSetup ? <CardDescription>This checklist unlocks the workspace.</CardDescription> : null}
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
                  <div className="space-y-5">
                    <div className="grid gap-3 md:grid-cols-4" aria-label="Workspace onboarding steps">
                      {memberSteps.map((step, index) => {
                        const complete = index < activeStepIndex;
                        const active = step.key === memberStep;
                        return (
                          <div
                            key={step.key}
                            className={[
                              "rounded-2xl border px-4 py-3 transition-colors",
                              active
                                ? "border-slate-900 bg-slate-900 text-white"
                                : complete
                                  ? "border-emerald-200 bg-emerald-50 text-emerald-900"
                                  : "border-slate-200 bg-slate-50 text-slate-600",
                            ].join(" ")}
                          >
                            <div className="flex items-center gap-3">
                              <span
                                className={[
                                  "inline-flex h-6 w-6 items-center justify-center rounded-full text-xs font-semibold",
                                  active ? "bg-white/15 text-white" : complete ? "bg-emerald-100 text-emerald-700" : "bg-white text-slate-500",
                                ].join(" ")}
                              >
                                {index + 1}
                              </span>
                              <div className="text-sm font-medium">{step.label}</div>
                            </div>
                          </div>
                        );
                      })}
                    </div>

                    {memberStep === "details" ? (
                      <div className="space-y-4">
                        <div>
                          <h2 className="text-lg font-semibold text-slate-900">Confirm your details</h2>
                        </div>
                        <div className="grid gap-4 md:grid-cols-2">
                          <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-4">
                            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Workspace</p>
                            <p className="mt-2 text-sm font-medium text-slate-900">{tenant?.name ?? pendingMembership.tenant_id}</p>
                          </div>
                          <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-4">
                            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Role</p>
                            <p className="mt-2 text-sm font-medium text-slate-900">{pendingMembership.role.replace(/_/g, " ")}</p>
                          </div>
                          <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-4 md:col-span-2">
                            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">Teams</p>
                            <p className="mt-2 text-sm text-slate-700">{teamList}</p>
                          </div>
                        </div>
                        <div className="flex justify-end">
                          <Button onClick={() => setMemberStep("experience")}>Continue to experience</Button>
                        </div>
                      </div>
                    ) : null}

                    {memberStep === "experience" ? (
                      <div className="space-y-4">
                        <div>
                          <h2 className="text-lg font-semibold text-slate-900">Select your experience</h2>
                        </div>
                        <div className="grid gap-4 md:grid-cols-2">
                          <button
                            type="button"
                            className={[
                              "rounded-2xl border px-5 py-5 text-left transition-colors",
                              selectedExperience === "non_technical"
                                ? "border-slate-900 bg-slate-900 text-white"
                                : "border-slate-200 bg-slate-50 text-slate-900 hover:border-slate-300",
                            ].join(" ")}
                            onClick={() => setSelectedExperience("non_technical")}
                          >
                            <div className="text-base font-semibold">Business view</div>
                            <div className="mt-2 text-sm opacity-80">Delivery progress and team updates.</div>
                          </button>
                          <button
                            type="button"
                            className={[
                              "rounded-2xl border px-5 py-5 text-left transition-colors",
                              selectedExperience === "technical"
                                ? "border-slate-900 bg-slate-900 text-white"
                                : "border-slate-200 bg-slate-50 text-slate-900 hover:border-slate-300",
                              !canChooseTechnicalExperience ? "cursor-not-allowed opacity-50" : "",
                            ].join(" ")}
                            onClick={() => canChooseTechnicalExperience && setSelectedExperience("technical")}
                            disabled={!canChooseTechnicalExperience}
                          >
                            <div className="text-base font-semibold">Technical view</div>
                            <div className="mt-2 text-sm opacity-80">Diagnostics and technical analytics.</div>
                          </button>
                        </div>
                        {!canChooseTechnicalExperience ? (
                          <p className="text-sm text-slate-600">Technical view is not available for your role.</p>
                        ) : null}
                        <div className="flex justify-between">
                          <Button variant="outline" onClick={() => setMemberStep("details")}>
                            Back
                          </Button>
                          <Button onClick={() => void handleContinueFromExperience()} disabled={experienceBusy}>
                            {experienceBusy ? "Saving..." : "Save and continue"}
                          </Button>
                        </div>
                      </div>
                    ) : null}

                    {memberStep === "discord" && discordReady ? (
                      <div className="space-y-4">
                        <div>
                          <h2 className="text-lg font-semibold text-slate-900">Join Discord</h2>
                        </div>
                        <ChecklistRow done={memberDiscordLinked} label="Link your Discord identity" />
                        <ChecklistRow done={memberDiscordJoined} label="Open the workspace Discord invite" />
                        {discordAction ? (
                          <div className="pt-1">
                            <Button variant="outline" onClick={discordAction.onClick} disabled={discordAction.disabled}>
                              <DiscordLogo className="mr-2 h-4 w-4" />
                              {discordAction.label}
                            </Button>
                          </div>
                        ) : null}
                        <div className="flex justify-between">
                          <Button variant="outline" onClick={() => setMemberStep("experience")}>
                            Back
                          </Button>
                          <Button onClick={() => setMemberStep("finish")}>Continue to finish</Button>
                        </div>
                      </div>
                    ) : null}

                    {memberStep === "finish" ? (
                      <div className="space-y-4">
                        <div>
                          <h2 className="text-lg font-semibold text-slate-900">Finish onboarding</h2>
                          <p className="mt-1 text-sm text-slate-600">You are ready to enter the workspace.</p>
                        </div>
                        <div className="flex justify-between">
                          <Button variant="outline" onClick={() => setMemberStep(discordReady ? "discord" : "experience")}>
                            Back
                          </Button>
                          <Button onClick={() => void handleComplete()} disabled={isCompleting}>
                            {isCompleting ? "Finishing..." : "Finish"}
                          </Button>
                        </div>
                      </div>
                    ) : null}
                  </div>
                </>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </main>
  );
}
