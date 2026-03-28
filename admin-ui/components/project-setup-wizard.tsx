"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, ArrowRight, Check, FolderKanban, Github, Layers, Search } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { createProject, getTenant, listGitHubRepositories, listJiraProjects } from "@/lib/api";
import { canManageProjects } from "@/lib/auth-routing";

type Step = 1 | 2 | 3 | 4;

const STEPS = [
  { num: 1 as Step, label: "Name your project", icon: FolderKanban },
  { num: 2 as Step, label: "Connect GitHub repository", icon: Github },
  { num: 3 as Step, label: "Link Jira project", icon: Layers },
  { num: 4 as Step, label: "Review and save", icon: Check },
];

export function ProjectSetupWizard() {
  const params = useParams<{ tenantId: string }>();
  const router = useRouter();
  const { credentials, ready, principal } = useAuth();

  const [step, setStep] = useState<Step>(1);
  const [busy, setBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [name, setName] = useState("");
  const [githubRepository, setGithubRepository] = useState("");
  const [jiraProjectKey, setJiraProjectKey] = useState("");
  const [repoOptions, setRepoOptions] = useState<string[]>([]);
  const [jiraOptions, setJiraOptions] = useState<string[]>([]);
  const allowProjectManagement = canManageProjects(principal, params.tenantId);

  async function loadOptions(auth = credentials) {
    if (!auth) return;
    setBusy(true);
    try {
      const tenant = await getTenant(auth, params.tenantId);
      if (!tenant.github.installation_id) {
        setRepoOptions([]);
        setStatusLine("GitHub App is not connected for this tenant. Connect GitHub first.");
      } else {
        const repos = await listGitHubRepositories(auth, params.tenantId);
        setRepoOptions(repos.map((repo) => repo.html_url));
        if (repos.length === 0) {
          setStatusLine("No repositories found for this tenant GitHub installation.");
        }
      }
      if (tenant.jira.connection_id) {
        const jiraProjects = await listJiraProjects(auth, tenant.jira.connection_id);
        setJiraOptions(jiraProjects.map((p) => p.key));
      } else {
        setJiraOptions([]);
        setStatusLine("Jira is not connected for this tenant. Connect Jira first.");
      }
    } catch (error) {
      setStatusLine(`Failed to load setup options: ${(error as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) void loadOptions(credentials);
  }, [ready, credentials, params.tenantId]);

  async function create() {
    if (!credentials) return;
    if (!allowProjectManagement) {
      setStatusLine("You do not have permission to create projects.");
      return;
    }
    if (!name.trim() || !githubRepository.trim() || !jiraProjectKey.trim()) {
      setStatusLine("Name, GitHub repository, and Jira project are required.");
      return;
    }
    setBusy(true);
    try {
      const created = await createProject(credentials, params.tenantId, {
        name: name.trim(),
        github_repository: githubRepository.trim(),
        jira_project_key: jiraProjectKey.trim().toUpperCase(),
        policy_overrides: {},
      });
      router.push(`/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(created.project_id)}`);
    } catch (error) {
      setStatusLine(`Create failed: ${(error as Error).message}`);
      setBusy(false);
    }
  }

  const currentStepDef = STEPS[step - 1];

  if (ready && credentials && !allowProjectManagement) {
    return (
      <div className="mx-auto w-full max-w-xl space-y-6 py-8">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Project setup unavailable</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4 text-sm text-muted-foreground">
            <p>You do not have permission to create or edit projects in this workspace.</p>
            <Button asChild variant="outline" size="sm">
              <Link href={`/${encodeURIComponent(params.tenantId)}/projects`}>
                <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />
                Back to Projects
              </Link>
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-xl space-y-6 py-8">
      {/* Back link */}
      <div>
        <Button asChild variant="ghost" size="sm" className="-ml-1">
          <Link href={`/${encodeURIComponent(params.tenantId)}/projects`}>
            <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />
            Back to Projects
          </Link>
        </Button>
      </div>

      {/* Step progress bar */}
      <div className="flex items-center gap-0">
        {STEPS.map((s, idx) => {
          const isCompleted = step > s.num;
          const isActive = step === s.num;
          return (
            <div key={s.num} className="flex flex-1 items-center">
              <div className="flex flex-col items-center gap-1">
                <div
                  className={`flex h-8 w-8 items-center justify-center rounded-full text-xs font-semibold transition-colors ${
                    isCompleted
                      ? "bg-success text-white"
                      : isActive
                      ? "bg-primary text-primary-foreground ring-2 ring-primary/30"
                      : "bg-muted text-muted-foreground"
                  }`}
                >
                  {isCompleted ? <Check className="h-3.5 w-3.5" /> : s.num}
                </div>
                <span
                  className={`hidden text-[10px] font-medium sm:block whitespace-nowrap ${
                    isActive ? "text-primary" : isCompleted ? "text-success" : "text-muted-foreground"
                  }`}
                >
                  {s.label}
                </span>
              </div>
              {idx < STEPS.length - 1 ? (
                <div className={`h-px flex-1 mx-1 ${step > s.num ? "bg-success" : "bg-border"}`} />
              ) : null}
            </div>
          );
        })}
      </div>

      {statusLine ? (
        <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p>
      ) : null}

      {/* Step card */}
      <Card>
        <CardHeader className="pb-4">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10">
              <currentStepDef.icon className="h-4 w-4 text-primary" />
            </div>
            <div>
              <p className="text-xs text-muted-foreground">Step {step} of 4</p>
              <CardTitle className="text-base">{currentStepDef.label}</CardTitle>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {step === 1 ? (
            <div className="space-y-2">
              <label className="text-sm font-medium">Project name</label>
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Mobile App"
                autoFocus
              />
              <p className="text-xs text-muted-foreground">Give your project a clear, recognisable name.</p>
            </div>
          ) : null}

          {step === 2 ? (
            <div className="space-y-2">
              <div className="flex items-center justify-between gap-2">
                <label className="text-sm font-medium">GitHub repository</label>
                <Button size="sm" variant="ghost" className="h-7 px-2 text-xs" onClick={() => void loadOptions()} disabled={busy}>
                  <Search className="mr-1 h-3 w-3" />
                  Reload
                </Button>
              </div>
              <Input
                list="wizard-repo-options"
                value={githubRepository}
                onChange={(e) => setGithubRepository(e.target.value)}
                placeholder="Choose or paste repository URL"
              />
              <datalist id="wizard-repo-options">
                {repoOptions.map((repo) => <option key={repo} value={repo} />)}
              </datalist>
              <p className="text-xs text-muted-foreground">Select the GitHub repository that this project maps to.</p>
            </div>
          ) : null}

          {step === 3 ? (
            <div className="space-y-2">
              <div className="flex items-center justify-between gap-2">
                <label className="text-sm font-medium">Jira project key</label>
                <Button size="sm" variant="ghost" className="h-7 px-2 text-xs" onClick={() => void loadOptions()} disabled={busy}>
                  <Search className="mr-1 h-3 w-3" />
                  Reload
                </Button>
              </div>
              <Input
                list="wizard-jira-options"
                value={jiraProjectKey}
                onChange={(e) => setJiraProjectKey(e.target.value.toUpperCase())}
                placeholder="e.g. PROJ"
              />
              <datalist id="wizard-jira-options">
                {jiraOptions.map((key) => <option key={key} value={key} />)}
              </datalist>
              <p className="text-xs text-muted-foreground">Choose the Jira project whose issues will trigger runs.</p>
            </div>
          ) : null}

          {step === 4 ? (
            <div className="space-y-3 text-sm">
              <div className="rounded-lg border bg-muted/30 px-4 py-3">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-1">Project name</p>
                <p className="font-medium">{name || <span className="text-muted-foreground italic">Not set</span>}</p>
              </div>
              <div className="rounded-lg border bg-muted/30 px-4 py-3">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-1">Repository</p>
                <p className="font-mono text-xs break-all">{githubRepository || <span className="text-muted-foreground italic">Not set</span>}</p>
              </div>
              <div className="rounded-lg border bg-muted/30 px-4 py-3">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-1">Jira project</p>
                <p className="font-medium">{jiraProjectKey || <span className="text-muted-foreground italic">Not set</span>}</p>
              </div>
            </div>
          ) : null}

          {/* Navigation footer */}
          <div className="flex items-center justify-between gap-2 border-t pt-4">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setStep((prev) => (prev === 1 ? 1 : ((prev - 1) as Step)))}
              disabled={step === 1}
            >
              <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />
              Back
            </Button>
            <span className="text-xs text-muted-foreground">Step {step} of 4</span>
            {step < 4 ? (
              <Button
                size="sm"
                onClick={() => setStep((prev) => ((prev + 1) as Step))}
                disabled={
                  (step === 1 && !name.trim()) ||
                  (step === 2 && !githubRepository.trim()) ||
                  (step === 3 && !jiraProjectKey.trim())
                }
              >
                Next
                <ArrowRight className="ml-1.5 h-3.5 w-3.5" />
              </Button>
            ) : (
              <Button size="sm" onClick={() => void create()} disabled={busy || !jiraProjectKey.trim()}>
                {busy ? "Saving…" : "Save Project"}
              </Button>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
