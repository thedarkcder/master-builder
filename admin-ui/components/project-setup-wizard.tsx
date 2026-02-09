"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, ArrowRight } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { createProject, getTenant, listGitHubRepositories, listJiraProjects } from "@/lib/api";

type Step = 1 | 2 | 3 | 4;

export function ProjectSetupWizard() {
  const params = useParams<{ tenantId: string }>();
  const router = useRouter();
  const { credentials, ready } = useAuth();

  const [step, setStep] = useState<Step>(1);
  const [busy, setBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("Create a project in four steps.");
  const [name, setName] = useState("");
  const [githubRepository, setGithubRepository] = useState("");
  const [jiraProjectKey, setJiraProjectKey] = useState("");
  const [repoOptions, setRepoOptions] = useState<string[]>([]);
  const [jiraOptions, setJiraOptions] = useState<string[]>([]);

  async function loadOptions(auth = credentials) {
    if (!auth) {
      return;
    }
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
        setJiraOptions(jiraProjects.map((project) => project.key));
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
    if (ready && credentials) {
      void loadOptions(credentials);
    }
  }, [ready, credentials, params.tenantId]);

  async function create() {
    if (!credentials) {
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
      router.push(`/tenants/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(created.project_id)}`);
    } catch (error) {
      setStatusLine(`Create failed: ${(error as Error).message}`);
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle>Project Setup Wizard</CardTitle>
            <CardDescription>Step {step} of 4</CardDescription>
          </div>
          <Button asChild variant="outline">
            <Link href={`/tenants/${encodeURIComponent(params.tenantId)}/projects`}>
              <ArrowLeft className="mr-2 h-4 w-4" />
              Back to Projects
            </Link>
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">{statusLine}</p>

        {step === 1 ? (
          <div className="space-y-2">
            <p className="text-sm font-medium">Project name</p>
            <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="Mobile App" />
          </div>
        ) : null}

        {step === 2 ? (
          <div className="space-y-2">
            <div className="flex items-center justify-between gap-2">
              <p className="text-sm font-medium">Select GitHub repository</p>
              <Button size="sm" variant="outline" onClick={() => void loadOptions()} disabled={busy}>
                Reload options
              </Button>
            </div>
            <Input
              list="wizard-repo-options"
              value={githubRepository}
              onChange={(event) => setGithubRepository(event.target.value)}
              placeholder="Choose repository"
            />
            <datalist id="wizard-repo-options">
              {repoOptions.map((repo) => (
                <option key={repo} value={repo} />
              ))}
            </datalist>
          </div>
        ) : null}

        {step === 3 ? (
          <div className="space-y-2">
            <div className="flex items-center justify-between gap-2">
              <p className="text-sm font-medium">Select Jira project</p>
              <Button size="sm" variant="outline" onClick={() => void loadOptions()} disabled={busy}>
                Reload options
              </Button>
            </div>
            <Input
              list="wizard-jira-options"
              value={jiraProjectKey}
              onChange={(event) => setJiraProjectKey(event.target.value.toUpperCase())}
              placeholder="Choose Jira project"
            />
            <datalist id="wizard-jira-options">
              {jiraOptions.map((key) => (
                <option key={key} value={key} />
              ))}
            </datalist>
          </div>
        ) : null}

        {step === 4 ? (
          <div className="space-y-3 text-sm">
            <p className="rounded-md border bg-muted/30 p-3">
              <strong>Project name:</strong> {name || "-"}
            </p>
            <p className="rounded-md border bg-muted/30 p-3">
              <strong>Repository:</strong> {githubRepository || "-"}
            </p>
            <p className="rounded-md border bg-muted/30 p-3">
              <strong>Jira project:</strong> {jiraProjectKey || "-"}
            </p>
          </div>
        ) : null}

        <div className="flex items-center justify-between gap-2 border-t pt-3">
          <Button variant="outline" onClick={() => setStep((prev) => (prev === 1 ? 1 : ((prev - 1) as Step)))}>
            <ArrowLeft className="mr-2 h-4 w-4" /> Back
          </Button>
          {step < 4 ? (
            <Button
              onClick={() => setStep((prev) => ((prev + 1) as Step))}
              disabled={
                (step === 1 && !name.trim()) || (step === 2 && !githubRepository.trim()) || (step === 3 && !jiraProjectKey.trim())
              }
            >
              Next <ArrowRight className="ml-2 h-4 w-4" />
            </Button>
          ) : (
            <Button onClick={() => void create()} disabled={busy || !jiraProjectKey.trim()}>
              {busy ? "Saving..." : "Save Project"}
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
