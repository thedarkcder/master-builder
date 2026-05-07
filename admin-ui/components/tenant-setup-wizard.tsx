"use client";

import { ArrowLeft, ArrowRight, Sparkles } from "lucide-react";

import { BasicsStep } from "@/components/tenant-setup/steps/basics-step";
import { DiscordStep } from "@/components/tenant-setup/steps/discord-step";
import { GitHubStep } from "@/components/tenant-setup/steps/github-step";
import { InviteUsersStep } from "@/components/tenant-setup/steps/invite-users-step";
import { JiraStep } from "@/components/tenant-setup/steps/jira-step";
import { ReposStep } from "@/components/tenant-setup/steps/repos-step";
import { ReviewStep } from "@/components/tenant-setup/steps/review-step";
import { STEP_ORDER, type WizardStepKey } from "@/components/tenant-setup/types";
import { useTenantSetupController, previewTenantId } from "@/components/tenant-setup/use-tenant-setup-controller";
import { WizardProgress } from "@/components/tenant-setup/wizard-progress";
import { Button } from "@/components/ui/button";

function WizardStatusBanner({ tone, line }: { tone: "info" | "success" | "error"; line: string }) {
  if (!line || tone === "info") {
    return null;
  }
  const toneClasses =
    tone === "error"
      ? "border-red-200 bg-red-50 text-red-800"
      : tone === "success"
        ? "border-emerald-200 bg-emerald-50 text-emerald-800"
        : "border-slate-200 bg-slate-50 text-slate-600";

  return <p className={`rounded-xl border px-4 py-3 text-sm ${toneClasses}`}>{line}</p>;
}

export function TenantSetupWizard({ stepKey }: { stepKey: WizardStepKey }) {
  const {
    values,
    setValues,
    textFields,
    setTextFields,
    status,
    saving,
    createdTenantId,
    selectedRepoUrl,
    setSelectedRepoUrl,
    installationRepos,
    jiraProjects,
    inviteEmail,
    setInviteEmail,
    inviteFullName,
    setInviteFullName,
    inviteRole,
    setInviteRole,
    sendingInvite,
    recentInvites,
    stepIndex,
    selectedProjectKeys,
    nextStep,
    previousStep,
    startAtlassianOAuth,
    loadJiraProjectsForConnection,
    toggleJiraProject,
    startGitHubInstallFlow,
    startDiscordInstallFlow,
    sendWorkspaceInvite,
    loadInstallationRepositories,
    saveTenantAndOpenWorkspace
  } = useTenantSetupController(stepKey);

  const tenantIdPreview = createdTenantId || previewTenantId(values.name);
  const step = STEP_ORDER[stepIndex] ?? STEP_ORDER[0];
  const isFinalStep = stepIndex >= STEP_ORDER.length - 1;
  const canGoBack = stepIndex > 0;
  const showStatusBanner = Boolean(status.line) && status.tone !== "info";

  return (
    <div className="overflow-hidden rounded-[32px] border border-slate-200 bg-white shadow-[0_22px_70px_rgba(15,23,42,0.08)]">
      <div className="grid lg:grid-cols-[188px_minmax(0,1fr)]">
        <aside className="border-b border-slate-200 bg-slate-50/60 px-5 py-6 lg:border-b-0 lg:border-r lg:px-4 lg:py-7">
          <div className="flex h-full flex-col gap-6">
            <div className="inline-flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">
              <Sparkles className="h-3.5 w-3.5 text-slate-400" />
              Workspace setup
            </div>

            <div className="rounded-2xl border border-slate-200 bg-white px-3 py-4">
              <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">
                Step {stepIndex + 1} of {STEP_ORDER.length}
              </div>
              <div className="mt-3">
                <WizardProgress stepIndex={stepIndex} />
              </div>
            </div>
          </div>
        </aside>

        <section className="min-w-0 px-6 py-7 sm:px-8 sm:py-9 lg:px-10 xl:px-12">
          <div className="mx-auto flex min-h-full max-w-5xl flex-col">
            <div className="space-y-2">
              <div className="space-y-1">
                <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">{step.label}</div>
                <h1 className="text-3xl font-semibold tracking-tight text-slate-950 sm:text-[2rem]">{step.title}</h1>
              </div>
              <p className="max-w-xl text-sm leading-6 text-slate-600">{step.description}</p>
            </div>

            {showStatusBanner ? (
              <div className="mt-5">
                <WizardStatusBanner tone={status.tone} line={status.line} />
              </div>
            ) : null}

            <div className="mt-8 flex-1">
              {stepKey === "basics" ? (
                <BasicsStep
                  name={values.name}
                  tenantIdPreview={tenantIdPreview}
                  onNameChange={(name) => setValues((prev) => ({ ...prev, name }))}
                />
              ) : null}

              {stepKey === "jira" ? (
                <JiraStep
                  connectionId={values.jira.connection_id}
                  projectKeysText={textFields.projectKeysText}
                  jiraProjects={jiraProjects}
                  selectedProjectKeys={selectedProjectKeys}
                  onProjectKeysTextChange={(value) => setTextFields((prev) => ({ ...prev, projectKeysText: value }))}
                  onToggleJiraProject={toggleJiraProject}
                  onStartJiraConnect={() => void startAtlassianOAuth()}
                  onLoadJiraProjects={() => void loadJiraProjectsForConnection()}
                />
              ) : null}

              {stepKey === "github" ? (
                <GitHubStep
                  createdTenantId={createdTenantId}
                  tenantIdPreview={tenantIdPreview}
                  installationId={values.github.installation_id}
                  onStartInstall={() => void startGitHubInstallFlow()}
                  onLoadRepositories={() => void loadInstallationRepositories()}
                />
              ) : null}

              {stepKey === "discord" ? (
                <DiscordStep
                  createdTenantId={createdTenantId}
                  tenantIdPreview={tenantIdPreview}
                  guildId={values.discord.guild_id}
                  installedAt={values.discord.installed_at}
                  onStartInstall={() => void startDiscordInstallFlow()}
                />
              ) : null}

              {stepKey === "repos" ? (
                <ReposStep
                  selectedRepoUrl={selectedRepoUrl}
                  repositories={installationRepos}
                  policy={values.policy}
                  onRepositoryChange={(url) => {
                    setSelectedRepoUrl(url);
                    setTextFields((prev) => ({ ...prev, githubRepositoryText: url }));
                  }}
                  onPolicyChange={(policy) => setValues((prev) => ({ ...prev, policy }))}
                />
              ) : null}

              {stepKey === "invite" ? (
                <InviteUsersStep
                  createdTenantId={createdTenantId}
                  tenantIdPreview={tenantIdPreview}
                  inviteEmail={inviteEmail}
                  inviteFullName={inviteFullName}
                  inviteRole={inviteRole}
                  sendingInvite={sendingInvite}
                  recentInvites={recentInvites}
                  onInviteEmailChange={(value) => setInviteEmail(value)}
                  onInviteFullNameChange={(value) => setInviteFullName(value)}
                  onInviteRoleChange={(value) => setInviteRole(value)}
                  onSendInvite={() => void sendWorkspaceInvite()}
                />
              ) : null}

              {stepKey === "review" ? (
                <ReviewStep
                  tenantDisplayId={tenantIdPreview}
                  tenantName={values.name}
                  jiraConnectionId={values.jira.connection_id}
                  jiraProjectKeys={textFields.projectKeysText}
                  githubInstallationId={values.github.installation_id}
                  repositoryUrl={textFields.githubRepositoryText}
                  saving={saving}
                  onSave={() => void saveTenantAndOpenWorkspace()}
                />
              ) : null}
            </div>

            <div className="mt-10 flex items-center justify-between gap-3 border-t border-slate-200 pt-6">
              <Button variant="outline" onClick={previousStep} disabled={!canGoBack}>
                <ArrowLeft className="mr-2 h-4 w-4" />
                Back
              </Button>
              {!isFinalStep ? (
                <Button onClick={() => void nextStep()} disabled={saving} className="min-w-[132px]">
                  Next
                  <ArrowRight className="ml-2 h-4 w-4" />
                </Button>
              ) : null}
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}

export type { WizardStepKey } from "@/components/tenant-setup/types";
