"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ArrowRight, Rocket } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  listProjectAppAnalysisRuns,
  listProjectAppReleases,
  listProjectApps,
  type Credentials,
  type ProjectAppAnalysisRunRecord,
  type ProjectAppDeploymentReleaseRecord,
  type ProjectAppRecord,
} from "@/lib/api/deployments";

type ProjectAppIndexPageProps = {
  tenantId: string;
  projectId: string;
  credentials: Credentials | null;
};

const APP_PAGE_SIZE = 25;

function isProjectDeployment(app: ProjectAppRecord): boolean {
  return app.source_path.trim() === ".";
}

function deploymentPath(tenantId: string, projectId: string, suffix = ""): string {
  const base = `/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/deployments`;
  return suffix ? `${base}/${suffix}` : base;
}

function projectDeploymentSettingsPath(tenantId: string, projectId: string): string {
  return `/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/deployment`;
}

function appStatusLabel(status: string): string {
  return status.replaceAll("_", " ");
}

function appStatusVariant(status: string): "default" | "secondary" | "outline" | "destructive" | "success" | "warning" {
  const normalized = status.trim().toLowerCase();
  if (normalized === "live" || normalized === "ready") return "success";
  if (normalized === "needs_pr_merge") return "warning";
  if (normalized === "failed") return "destructive";
  if (normalized === "deploying") return "default";
  return "outline";
}

function appLatestReleaseDisplayName(app: ProjectAppRecord): string | null {
  const releaseName = app.latest_release_name?.trim();
  if (releaseName) {
    return releaseName;
  }
  const gitRef = app.latest_release_git_ref?.trim();
  const commitSha = app.latest_release_commit_sha?.trim();
  if (gitRef && commitSha) {
    return `${gitRef} @ ${commitSha.slice(0, 8)}`;
  }
  return null;
}

function branchLabelForGitRef(gitRef: string | null | undefined, releaseKind?: string): string {
  if (releaseKind === "run_preview") return "Preview";
  const normalizedRef = gitRef?.trim().toLowerCase() ?? "";
  const tail = normalizedRef.split("/").filter(Boolean).at(-1) ?? normalizedRef;
  if (tail === "stage" || tail === "staging" || tail.startsWith("stage-") || tail.startsWith("staging-")) {
    return "Stage";
  }
  if (tail === "main" || tail === "master" || tail.startsWith("main-") || tail.startsWith("master-")) {
    return "Main";
  }
  return "Branch";
}

function appBranchLabel(app: ProjectAppRecord): string {
  return branchLabelForGitRef(app.latest_release_git_ref);
}

function deploymentTitle(app: ProjectAppRecord, index: number): string {
  return appLatestReleaseDisplayName(app) ?? `Deployment ${index + 1}`;
}

function deploymentSummary(app: ProjectAppRecord): string {
  const releaseName = appLatestReleaseDisplayName(app);
  if (releaseName) return `Latest release ${releaseName}`;
  return "Awaiting first GitHub release";
}

function isActivePreviewRelease(release: ProjectAppDeploymentReleaseRecord): boolean {
  if (release.release_kind !== "run_preview" || release.destroyed_at) {
    return false;
  }
  const normalizedStatus = release.status.trim().toLowerCase();
  return !["destroyed", "failed", "rolled_back"].includes(normalizedStatus);
}

function activePreviewReleasesByBranch(releases: ProjectAppDeploymentReleaseRecord[]): ProjectAppDeploymentReleaseRecord[] {
  const byBranch = new Map<string, ProjectAppDeploymentReleaseRecord>();
  const newestFirst = [...releases].sort((left, right) => right.created_at.localeCompare(left.created_at));
  for (const release of newestFirst) {
    if (!isActivePreviewRelease(release)) {
      continue;
    }
    const key = release.git_ref.trim() || release.source_run_id || release.release_id;
    if (!byBranch.has(key)) {
      byBranch.set(key, release);
    }
  }
  return [...byBranch.values()];
}

function releaseBranchLabel(release: ProjectAppDeploymentReleaseRecord): string {
  return branchLabelForGitRef(release.git_ref, release.release_kind);
}

function releaseDisplayName(release: ProjectAppDeploymentReleaseRecord): string {
  return `${release.git_ref} @ ${release.commit_sha.slice(0, 8)}`;
}

function previewIssueKey(release: ProjectAppDeploymentReleaseRecord): string | null {
  const explicit = release.source_issue_key?.trim().toUpperCase();
  if (explicit) {
    return explicit;
  }
  const matches = [...release.git_ref.toUpperCase().matchAll(/\b[A-Z][A-Z0-9_]+-\d+\b/g)];
  return matches.at(-1)?.[0] ?? null;
}

function previewIssueTitle(release: ProjectAppDeploymentReleaseRecord): string {
  const issueKey = previewIssueKey(release);
  const summary = release.source_issue_summary?.trim();
  if (issueKey && summary) {
    return `${issueKey}: ${summary}`;
  }
  if (issueKey) {
    return issueKey;
  }
  return "Ticket context unavailable";
}

function previewPrimaryUrl(release: ProjectAppDeploymentReleaseRecord): string | null {
  return release.service_urls.find((serviceUrl) => serviceUrl.service_kind === "website")?.url ?? release.service_urls[0]?.url ?? null;
}

type PreviewRow = {
  app: ProjectAppRecord;
  release: ProjectAppDeploymentReleaseRecord;
};

export function ProjectAppIndexPage({ tenantId, projectId, credentials }: ProjectAppIndexPageProps) {
  const [apps, setApps] = useState<ProjectAppRecord[]>([]);
  const [previewRows, setPreviewRows] = useState<PreviewRow[]>([]);
  const [latestSetupRun, setLatestSetupRun] = useState<ProjectAppAnalysisRunRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [statusLine, setStatusLine] = useState("");
  const [page, setPage] = useState(1);
  const [hasNextPage, setHasNextPage] = useState(false);

  useEffect(() => {
    if (!credentials) {
      setLoading(false);
      return;
    }
    const activeCredentials = credentials;
    let cancelled = false;
    setLoading(true);
    setStatusLine("");
    async function loadDeployments() {
      const loaded = await listProjectApps(activeCredentials, tenantId, projectId, {
        limit: APP_PAGE_SIZE + 1,
        offset: (page - 1) * APP_PAGE_SIZE,
      });
      const projectDeployments = loaded.filter(isProjectDeployment);
      const visibleDeployments = projectDeployments.slice(0, APP_PAGE_SIZE);
      const releaseGroups = await Promise.all(
        visibleDeployments.map(async (app) => ({
          app,
          releases: await listProjectAppReleases(activeCredentials, tenantId, projectId, app.app_id),
        })),
      );
      const latestRuns = loaded.length === 0 ? await listProjectAppAnalysisRuns(activeCredentials, tenantId, projectId) : [];
      return {
        apps: projectDeployments,
        previewRows: releaseGroups.flatMap(({ app, releases }) =>
          activePreviewReleasesByBranch(releases).map((release) => ({ app, release })),
        ),
        latestSetupRun:
          latestRuns.find((run) => run.request_payload?.analysis_source === "deployment_setup") ?? null,
      };
    }
    void loadDeployments()
      .then(({ apps: loaded, previewRows: loadedPreviewRows, latestSetupRun: loadedSetupRun }) => {
        if (cancelled) return;
        setApps(loaded.slice(0, APP_PAGE_SIZE));
        setPreviewRows(loadedPreviewRows);
        setHasNextPage(loaded.length > APP_PAGE_SIZE);
        setLatestSetupRun(loadedSetupRun);
      })
      .catch((error) => {
        if (cancelled) return;
        setStatusLine(`Failed to load deployments: ${(error as Error).message}`);
        setPreviewRows([]);
        setHasNextPage(false);
        setLatestSetupRun(null);
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [credentials, tenantId, projectId, page]);

  useEffect(() => {
    setPage(1);
  }, [tenantId, projectId]);

  if (loading) {
    return (
      <Card>
        <CardContent className="flex min-h-[280px] items-center justify-center text-sm text-muted-foreground">
          Loading deployments...
        </CardContent>
      </Card>
    );
  }

  if (apps.length === 0) {
    const setupStatus = latestSetupRun?.status?.trim().toLowerCase() ?? "";
    const setupIsActive = setupStatus === "queued" || setupStatus === "running";
    const setupFailed = setupStatus === "failed";
    return (
      <div className="mx-auto flex min-h-[520px] max-w-xl flex-col items-center justify-center px-6 text-center">
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-primary/10 text-primary">
          <Rocket className="h-7 w-7" />
        </div>
        <h2 className="mt-6 text-2xl font-semibold tracking-tight">
          {setupIsActive ? "Deployment setup is running" : setupFailed ? "Deployment setup failed" : "No deployments yet"}
        </h2>
        <p className="mt-3 text-sm leading-6 text-muted-foreground">
          {setupIsActive
            ? "MB is analyzing the selected branch, preparing deployable services, and creating the first release."
            : setupFailed
              ? latestSetupRun?.error || latestSetupRun?.last_error || "Deployment setup failed before a deployable service was created."
              : "Deployment releases appear here after GitHub events match this project&apos;s deployment policy."}
        </p>
        {setupIsActive ? null : (
          <Button asChild className="mt-7">
            <Link href={projectDeploymentSettingsPath(tenantId, projectId)}>
              Open deployment settings
            </Link>
          </Button>
        )}
        {statusLine ? <p className="mt-4 text-sm text-destructive">{statusLine}</p> : null}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {statusLine ? <p className="rounded-xl border bg-muted/30 px-4 py-3 text-sm text-destructive">{statusLine}</p> : null}
      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="divide-y">
          {apps.map((app, index) => (
            <Link
              key={app.app_id}
              href={deploymentPath(tenantId, projectId, encodeURIComponent(app.app_id))}
              className="grid gap-3 px-5 py-4 transition-colors hover:bg-muted/40 md:grid-cols-[minmax(0,1fr)_auto] md:items-center"
            >
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="outline">{appBranchLabel(app)}</Badge>
                  <p className="truncate text-sm font-semibold">{deploymentTitle(app, index)}</p>
                  <Badge variant={appStatusVariant(app.latest_release_status || app.status)}>
                    {appStatusLabel(app.latest_release_status || app.status)}
                  </Badge>
                </div>
                <p className="mt-1 truncate text-xs text-muted-foreground">{deploymentSummary(app)}</p>
                {app.last_error ? <p className="mt-2 text-xs text-destructive">{app.last_error}</p> : null}
              </div>
              <span className="inline-flex items-center text-sm font-medium text-primary">
                Manage
                <ArrowRight className="ml-2 h-4 w-4" />
              </span>
            </Link>
          ))}
          {previewRows.map(({ app, release }) => (
            <Link
              key={release.release_id}
              href={`${deploymentPath(tenantId, projectId, encodeURIComponent(app.app_id))}?release=${encodeURIComponent(release.release_id)}`}
              className="grid w-full gap-3 px-5 py-4 text-left transition-colors hover:bg-muted/40 md:grid-cols-[minmax(0,1fr)_auto] md:items-center"
            >
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="outline">{releaseBranchLabel(release)}</Badge>
                  <p className="truncate text-sm font-semibold">{previewIssueTitle(release)}</p>
                  <Badge variant={appStatusVariant(release.status)}>{appStatusLabel(release.status)}</Badge>
                </div>
                <p className="mt-1 truncate text-xs text-muted-foreground">{releaseDisplayName(release)}</p>
                {release.last_error ? <p className="mt-2 text-xs text-destructive">{release.last_error}</p> : null}
              </div>
              <span className="inline-flex items-center text-sm font-medium text-primary">
                Details
                <ArrowRight className="ml-2 h-4 w-4" />
              </span>
            </Link>
          ))}
        </div>
        {page > 1 || hasNextPage ? (
          <div className="flex items-center justify-between border-t px-5 py-3">
            <span className="text-xs text-muted-foreground">Page {page}</span>
            <div className="flex items-center gap-1">
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                onClick={() => setPage((current) => Math.max(1, current - 1))}
                disabled={loading || page <= 1}
              >
                Prev
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                onClick={() => setPage((current) => current + 1)}
                disabled={loading || !hasNextPage}
              >
                Next
              </Button>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
