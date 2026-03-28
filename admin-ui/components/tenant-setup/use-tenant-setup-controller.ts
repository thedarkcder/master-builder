"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { useAuth } from "@/components/auth-provider";
import {
  createTenant,
  getTenant,
  listGitHubRepositories,
  listJiraProjects,
  startDiscordInstall,
  startGitHubInstall,
  startJiraConnect,
  updateTenant,
  type GitHubRepositoryRecord,
  type JiraProjectRecord
} from "@/lib/api";
import {
  defaultTenantFormValues,
  formValuesToTextFields,
  recordToFormValues,
  splitCsv,
  toCreatePayload,
  toUpdatePayload,
  type TenantFormTextFields,
  type TenantFormValues
} from "@/lib/tenant-form";

import { STEP_ORDER, type WizardStepKey } from "@/components/tenant-setup/types";
import { persistWizardDraft, previewTenantId, readWizardDraft, type WizardDraft } from "@/components/tenant-setup/wizard-draft";

type StatusTone = "info" | "success" | "error";

function statusTone(statusLine: string): StatusTone {
  const normalized = statusLine.toLowerCase();
  if (normalized.includes("failed") || normalized.includes("unable") || normalized.includes("missing")) {
    return "error";
  }
  if (normalized.includes("loaded") || normalized.includes("saved") || normalized.includes("connected") || normalized.includes("created")) {
    return "success";
  }
  return "info";
}

export function useTenantSetupController(stepKey: WizardStepKey) {
  const { credentials } = useAuth();
  const searchParams = useSearchParams();
  const router = useRouter();

  const [values, setValues] = useState<TenantFormValues>(() => {
    const draft = readWizardDraft();
    if (draft?.values) {
      return draft.values as TenantFormValues;
    }
    return defaultTenantFormValues();
  });
  const [textFields, setTextFields] = useState<TenantFormTextFields>(() => {
    const draft = readWizardDraft();
    if (draft?.textFields) {
      return draft.textFields as TenantFormTextFields;
    }
    if (draft?.values) {
      return formValuesToTextFields(draft.values as TenantFormValues);
    }
    return formValuesToTextFields(defaultTenantFormValues());
  });
  const [statusLine, setStatusLine] = useState("");
  const [saving, setSaving] = useState(false);
  const [createdTenantId, setCreatedTenantId] = useState(() => {
    const draft = readWizardDraft();
    return typeof draft?.createdTenantId === "string" ? draft.createdTenantId : "";
  });

  const [installationRepos, setInstallationRepos] = useState<GitHubRepositoryRecord[]>([]);
  const [selectedRepoUrl, setSelectedRepoUrl] = useState(() => {
    const draft = readWizardDraft();
    return typeof draft?.selectedRepoUrl === "string" ? draft.selectedRepoUrl : "";
  });
  const [jiraProjects, setJiraProjects] = useState<JiraProjectRecord[]>([]);

  const stepIndex = STEP_ORDER.findIndex((step) => step.key === stepKey);

  const advanceValidationError = useMemo(() => {
    if (!values.name.trim()) {
      return "Tenant name is required.";
    }
    if (stepKey === "basics") {
      return null;
    }
    if (stepKey === "jira") {
      if (!values.jira.connection_id?.trim()) {
        return "Connect Jira before continuing.";
      }
      if (!textFields.projectKeysText.trim()) {
        return "Select at least one Jira project key before continuing.";
      }
      return null;
    }
    if (stepKey === "github") {
      if (!values.github.installation_id) {
        return "Install the GitHub App before continuing.";
      }
      return null;
    }
    if (stepKey === "discord") {
      if (!values.discord.guild_id?.trim()) {
        return "Install the Discord bot before continuing.";
      }
      return null;
    }
    if (stepKey === "repos") {
      if (!textFields.githubRepositoryText.trim()) {
        return "Select a GitHub repository before continuing.";
      }
      return null;
    }
    return null;
  }, [stepKey, textFields.githubRepositoryText, textFields.projectKeysText, values]);

  const status = useMemo(
    () => ({ line: statusLine, tone: statusTone(statusLine) }),
    [statusLine]
  );

  useEffect(() => {
    const draft: WizardDraft = {
      values,
      textFields,
      createdTenantId,
      selectedRepoUrl
    };
    persistWizardDraft(draft);
  }, [values, textFields, createdTenantId, selectedRepoUrl]);

  useEffect(() => {
    const tenantIdParam = searchParams.get("tenant_id");
    if (!credentials || typeof tenantIdParam !== "string" || tenantIdParam.length === 0) {
      return;
    }
    const auth = credentials;
    const tenantId = tenantIdParam;

    let ignore = false;
    async function loadTenantFromQuery(): Promise<void> {
      try {
        const record = await getTenant(auth, tenantId);
        if (ignore) {
          return;
        }
        const form = recordToFormValues(record);
        setValues(form);
        setTextFields(formValuesToTextFields(form));
        setCreatedTenantId(record.tenant_id);
        setSelectedRepoUrl(form.repos.github_repository ?? "");
        if (searchParams.get("github_install") === "success") {
          setStatusLine("GitHub App install completed. Load repositories to continue setup.");
        } else if (searchParams.get("discord_install") === "success") {
          setStatusLine("Discord bot install completed. Confirm the onboarding channel and invite settings.");
        } else {
          setStatusLine("");
        }
      } catch (error) {
        if (!ignore) {
          setStatusLine(`Failed to load tenant: ${(error as Error).message}`);
        }
      }
    }

    void loadTenantFromQuery();
    return () => {
      ignore = true;
    };
  }, [credentials, searchParams]);

  useEffect(() => {
    const connectionId = searchParams.get("jira_connection_id");
    if (!connectionId) {
      return;
    }
    setValues((prev) => ({ ...prev, jira: { ...prev.jira, connection_id: connectionId } }));
    if (searchParams.get("jira_oauth") === "success") {
      setStatusLine("Jira OAuth connected. Load Jira projects and select project keys.");
    }
  }, [searchParams]);

  async function ensureTenantCreated(): Promise<string | null> {
    if (!credentials) {
      setStatusLine("Missing API credentials.");
      return null;
    }
    if (!values.name.trim()) {
      setStatusLine("Tenant name is required.");
      return null;
    }
    if (createdTenantId) {
      return createdTenantId;
    }

    setSaving(true);
    try {
      const payload = toCreatePayload(values, textFields);
      const created = await createTenant(credentials, payload);
      const form = recordToFormValues(created);
      setValues(form);
      setTextFields(formValuesToTextFields(form));
      setCreatedTenantId(created.tenant_id);
      setStatusLine(`Created tenant ${created.tenant_id}.`);
      return created.tenant_id;
    } catch (error) {
      setStatusLine(`Create failed: ${(error as Error).message}`);
      return null;
    } finally {
      setSaving(false);
    }
  }

  async function startJiraOAuth() {
    if (!credentials) {
      return;
    }
    try {
      const result = await startJiraConnect(credentials, { returnTo: "wizard" });
      window.location.href = result.authorize_url;
    } catch (error) {
      setStatusLine(`Unable to start Jira OAuth: ${(error as Error).message}`);
    }
  }

  async function loadJiraProjectsForConnection() {
    if (!credentials || !values.jira.connection_id) {
      return;
    }
    try {
      const projects = await listJiraProjects(credentials, values.jira.connection_id);
      setJiraProjects(projects);
      if (projects.length === 0) {
        setStatusLine("Connected Jira site is valid, but no projects were returned.");
        return;
      }

      const existing = new Set(splitCsv(textFields.projectKeysText));
      const selected = projects.map((project) => project.key).filter((projectKey) => existing.has(projectKey));
      if (selected.length === 0) {
        setTextFields((prev) => ({
          ...prev,
          projectKeysText: projects.map((project) => project.key).join(", ")
        }));
      }

      setStatusLine(`Loaded ${projects.length} Jira project option(s).`);
    } catch (error) {
      setStatusLine(`Unable to load Jira projects: ${(error as Error).message}`);
    }
  }

  function toggleJiraProject(projectKey: string) {
    const current = new Set(splitCsv(textFields.projectKeysText));
    if (current.has(projectKey)) {
      current.delete(projectKey);
    } else {
      current.add(projectKey);
    }
    const ordered = jiraProjects.map((project) => project.key).filter((key) => current.has(key));
    const extras = [...current].filter((key) => !ordered.includes(key)).sort();
    setTextFields((prev) => ({ ...prev, projectKeysText: [...ordered, ...extras].join(", ") }));
  }

  async function startGitHubInstallFlow() {
    if (!credentials) {
      return;
    }
    const tenantId = await ensureTenantCreated();
    if (!tenantId) {
      return;
    }
    try {
      const result = await startGitHubInstall(credentials, tenantId, { returnTo: "wizard" });
      window.location.href = result.install_url;
    } catch (error) {
      setStatusLine(`Unable to start GitHub App install: ${(error as Error).message}`);
    }
  }

  async function startDiscordInstallFlow() {
    if (!credentials) {
      return;
    }
    const tenantId = await ensureTenantCreated();
    if (!tenantId) {
      return;
    }
    try {
      const result = await startDiscordInstall(credentials, tenantId, { returnTo: "wizard" });
      window.location.href = result.install_url;
    } catch (error) {
      setStatusLine(`Unable to start Discord bot install: ${(error as Error).message}`);
    }
  }

  async function loadInstallationRepositories() {
    if (!credentials || !createdTenantId) {
      return;
    }
    try {
      const repositories = await listGitHubRepositories(credentials, createdTenantId);
      setInstallationRepos(repositories);
      if (repositories.length === 0) {
        setStatusLine("GitHub installation connected, but no repositories are accessible.");
        return;
      }
      const existing = textFields.githubRepositoryText.trim();
      const selected = repositories.find((repo) => repo.html_url === existing);
      if (selected) {
        setSelectedRepoUrl(selected.html_url);
      } else {
        const defaultRepo = repositories[0]?.html_url ?? "";
        setSelectedRepoUrl(defaultRepo);
        setTextFields((prev) => ({ ...prev, githubRepositoryText: defaultRepo }));
      }
      setStatusLine(`Loaded ${repositories.length} repository option(s) from GitHub installation.`);
    } catch (error) {
      setStatusLine(`Unable to load repositories: ${(error as Error).message}`);
    }
  }

  useEffect(() => {
    if (stepKey !== "repos" || !values.github.installation_id || installationRepos.length > 0) {
      return;
    }
    void loadInstallationRepositories();
  }, [stepKey, values.github.installation_id, installationRepos.length]);

  async function saveTenant(): Promise<boolean> {
    if (!credentials) {
      return false;
    }
    const tenantId = await ensureTenantCreated();
    if (!tenantId) {
      return false;
    }

    setSaving(true);
    try {
      const payload = toUpdatePayload(values, textFields);
      const updated = await updateTenant(credentials, tenantId, payload);
      const form = recordToFormValues(updated);
      setValues(form);
      setTextFields(formValuesToTextFields(form));
      setStatusLine(`Saved tenant ${updated.tenant_id}.`);
      return true;
    } catch (error) {
      setStatusLine(`Save failed: ${(error as Error).message}`);
      return false;
    } finally {
      setSaving(false);
    }
  }

  function goToStep(index: number) {
    persistWizardDraft({
      values,
      textFields,
      createdTenantId,
      selectedRepoUrl
    });
    const next = STEP_ORDER[index];
    router.push(`/tenants/new/${next.key}`);
  }

  async function nextStep() {
    if (advanceValidationError) {
      setStatusLine(advanceValidationError);
      return;
    }

    if (stepKey === "jira") {
      const tenantId = await ensureTenantCreated();
      if (!tenantId) {
        return;
      }
    }

    if (stepKey === "discord" || stepKey === "repos") {
      const saved = await saveTenant();
      if (!saved) {
        return;
      }
    }

    if (stepIndex < STEP_ORDER.length - 1) {
      goToStep(stepIndex + 1);
    }
  }

  function previousStep() {
    if (stepIndex > 0) {
      goToStep(stepIndex - 1);
    }
  }

  const selectedProjectKeys = new Set(splitCsv(textFields.projectKeysText));

  return {
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
  };
}

export { previewTenantId };
