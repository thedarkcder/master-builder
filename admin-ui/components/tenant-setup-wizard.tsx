"use client";

import Link from "next/link";
import { ArrowLeft, ArrowRight } from "lucide-react";

import { BasicsStep } from "@/components/tenant-setup/steps/basics-step";
import { DiscordStep } from "@/components/tenant-setup/steps/discord-step";
import { GitHubStep } from "@/components/tenant-setup/steps/github-step";
import { JiraStep } from "@/components/tenant-setup/steps/jira-step";
import { ReposStep } from "@/components/tenant-setup/steps/repos-step";
import { ReviewStep } from "@/components/tenant-setup/steps/review-step";
import { type WizardStepKey } from "@/components/tenant-setup/types";
import { useTenantSetupController, previewTenantId } from "@/components/tenant-setup/use-tenant-setup-controller";
import { WizardProgress } from "@/components/tenant-setup/wizard-progress";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

function WizardStatusBanner({ tone, line }: { tone: "info" | "success" | "error"; line: string }) {
  const toneClasses =
    tone === "error"
      ? "border-red-300 bg-red-50 text-red-800"
      : tone === "success"
        ? "border-emerald-300 bg-emerald-50 text-emerald-800"
        : "border-muted bg-muted/40 text-muted-foreground";

  return <p className={`rounded-md border px-3 py-2 text-sm ${toneClasses}`}>{line}</p>;
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
    stepIndex,
    selectedProjectKeys,
    nextStep,
    previousStep,
    startJiraOAuth,
    loadJiraProjectsForConnection,
    toggleJiraProject,
    startGitHubInstallFlow,
    startDiscordInstallFlow,
    loadInstallationRepositories,
    saveTenant
  } = useTenantSetupController(stepKey);

  const tenantIdPreview = createdTenantId || previewTenantId(values.name);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Tenant Setup Wizard</CardTitle>
        <CardDescription>
          Connect Jira and GitHub, then apply discovered projects and repositories into tenant policy.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <WizardProgress stepIndex={stepIndex} />

        <WizardStatusBanner tone={status.tone} line={status.line} />

        <p className="rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          Manage credentials and webhook secrets in{" "}
          <Link href="/secrets" className="font-medium text-primary underline underline-offset-2">
            Secrets
          </Link>{" "}
          before running Jira/GitHub connection checks.
        </p>

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
            onStartJiraConnect={() => void startJiraOAuth()}
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
            onboardingChannelId={values.discord.onboarding_channel_id}
            inviteExpirySeconds={values.discord.onboarding_invite_expires_in_seconds}
            inviteMaxUses={values.discord.onboarding_invite_max_uses}
            onStartInstall={() => void startDiscordInstallFlow()}
            onOnboardingChannelChange={(value) =>
              setValues((prev) => ({
                ...prev,
                discordEnabled: true,
                discord: { ...prev.discord, onboarding_channel_id: value || null }
              }))
            }
            onInviteExpiryChange={(value) =>
              setValues((prev) => ({
                ...prev,
                discordEnabled: true,
                discord: {
                  ...prev.discord,
                  onboarding_invite_expires_in_seconds: value.trim() ? Number(value) : null
                }
              }))
            }
            onInviteMaxUsesChange={(value) =>
              setValues((prev) => ({
                ...prev,
                discordEnabled: true,
                discord: {
                  ...prev.discord,
                  onboarding_invite_max_uses: value.trim() ? Number(value) : null
                }
              }))
            }
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

        {stepKey === "review" ? (
          <ReviewStep
            tenantDisplayId={tenantIdPreview}
            tenantName={values.name}
            jiraConnectionId={values.jira.connection_id}
            jiraProjectKeys={textFields.projectKeysText}
            githubInstallationId={values.github.installation_id}
            repositoryUrl={textFields.githubRepositoryText}
            saving={saving}
            onSave={() => void saveTenant()}
          />
        ) : null}

        <div className="flex items-center justify-between gap-2 border-t pt-3">
          <Button variant="outline" onClick={previousStep} disabled={stepIndex === 0}>
            <ArrowLeft className="mr-2 h-4 w-4" /> Back
          </Button>
          <Button onClick={() => void nextStep()} disabled={stepIndex >= 5 || saving}>
            Next <ArrowRight className="ml-2 h-4 w-4" />
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export type { WizardStepKey } from "@/components/tenant-setup/types";
