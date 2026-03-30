import { expect, test } from "@playwright/test";

import {
  fulfillJson,
  installBffApiMocks,
  makePlatformAdminPrincipal,
  makeTenant,
  seedAdminSession,
} from "./support/admin-ui";
import { archiveTenant } from "./support/live-backend";

type RuntimeMatrixEntry = {
  runtimeKind: string;
  createModelValue: string;
  updateModelValue: string;
  usesPresetModel: boolean;
  expectedCliCommand?: string;
  expectedBaseUrl?: string;
  requiresApiKey?: boolean;
  allowsOptionalApiKey?: boolean;
};

const RUNTIME_FORM_MATRIX: RuntimeMatrixEntry[] = [
  {
    runtimeKind: "codex_cli",
    createModelValue: "gpt-5.4",
    updateModelValue: "gpt-5.3-codex",
    usesPresetModel: true,
    expectedCliCommand: "codex",
  },
  {
    runtimeKind: "chat_cli",
    createModelValue: "gpt-5.4-mini",
    updateModelValue: "gpt-4.1",
    usesPresetModel: true,
    expectedCliCommand: "chat",
  },
  {
    runtimeKind: "claude_cli",
    createModelValue: "claude-sonnet-4-0",
    updateModelValue: "claude-opus-4-0",
    usesPresetModel: true,
    expectedCliCommand: "claude",
  },
  {
    runtimeKind: "openai",
    createModelValue: "gpt-5.4",
    updateModelValue: "gpt-5.4-mini",
    usesPresetModel: true,
    expectedBaseUrl: "https://api.openai.com/v1",
    requiresApiKey: true,
  },
  {
    runtimeKind: "claude",
    createModelValue: "claude-sonnet-4-0",
    updateModelValue: "claude-opus-4-0",
    usesPresetModel: true,
    expectedBaseUrl: "https://api.anthropic.com",
    requiresApiKey: true,
  },
  {
    runtimeKind: "llama_cpp",
    createModelValue: "llama3.1:8b",
    updateModelValue: "llama3.1:70b",
    usesPresetModel: false,
    expectedBaseUrl: "http://localhost:8080/v1",
    allowsOptionalApiKey: true,
  },
  {
    runtimeKind: "lm_studio",
    createModelValue: "local-model",
    updateModelValue: "local-model-v2",
    usesPresetModel: false,
    expectedBaseUrl: "http://localhost:1234/v1",
    allowsOptionalApiKey: true,
  },
];

function makeModelCatalog(runtimeKind: string) {
  const presetModels: Record<string, { id: string; label: string; description: string }[]> = {
    codex_cli: [
      { id: "gpt-5.4", label: "GPT-5.4", description: "Codex default" },
      { id: "gpt-5.3-codex", label: "GPT-5.3 Codex", description: "Alternate Codex preset" },
    ],
    chat_cli: [
      { id: "gpt-5.4-mini", label: "GPT-5.4 Mini", description: "Chat default" },
      { id: "gpt-4.1", label: "GPT-4.1", description: "Alternate chat preset" },
    ],
    claude_cli: [
      { id: "claude-sonnet-4-0", label: "Claude Sonnet 4.0", description: "Claude CLI default" },
      { id: "claude-opus-4-0", label: "Claude Opus 4.0", description: "Alternate Claude CLI preset" },
    ],
    openai: [
      { id: "gpt-5.4", label: "GPT-5.4", description: "OpenAI default" },
      { id: "gpt-5.4-mini", label: "GPT-5.4 Mini", description: "Alternate OpenAI preset" },
    ],
    claude: [
      { id: "claude-sonnet-4-0", label: "Claude Sonnet 4.0", description: "Anthropic default" },
      { id: "claude-opus-4-0", label: "Claude Opus 4.0", description: "Alternate Anthropic preset" },
    ],
    llama_cpp: [],
    lm_studio: [],
  };
  return {
    default_model: presetModels[runtimeKind]?.[0]?.id ?? null,
    default_reasoning_effort: "medium",
    runtime_kind: runtimeKind,
    profile_name: null,
    models: presetModels[runtimeKind] ?? [],
    reasoning_efforts: [
      { id: "medium", label: "Medium", description: "Balanced" },
      { id: "low", label: "Low", description: "Fast" },
      { id: "high", label: "High", description: "Deep" },
    ],
  };
}

function validateRuntimePayload(payload: Record<string, unknown>): string | null {
  const runtimeKind = String(payload.runtime_kind || "");
  const cliCommand = String(payload.cli_command || "").trim();
  const model = String(payload.model || "").trim();
  const baseUrl = String(payload.base_url || "").trim();
  const apiKeySecretRef = String(payload.api_key_secret_ref || "").trim();

  if (!model) return "model is required";
  if (runtimeKind === "codex_cli" || runtimeKind === "chat_cli" || runtimeKind === "claude_cli") {
    if (!cliCommand) return "cli_command is required for CLI runtimes";
    return null;
  }
  if (!baseUrl) return `base_url is required for runtime ${runtimeKind}`;
  if (runtimeKind === "openai" || runtimeKind === "claude") {
    if (!apiKeySecretRef) return `api_key_secret_ref is required for runtime ${runtimeKind}`;
  }
  return null;
}

test("hydrates a stored valid admin session and opens platform admin home", async ({ page }) => {
  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, makePlatformAdminPrincipal()),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants",
      handler: (route) => fulfillJson(route, [makeTenant()]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/runs",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/secrets",
      handler: (route) => fulfillJson(route, []),
    },
  ]);

  await page.goto("/dashboard");

  await expect(page.getByRole("heading", { name: "Operations Overview" })).toBeVisible();
});

test("shows a dedicated Status page for platform services", async ({ page }) => {
  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, makePlatformAdminPrincipal()),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants",
      handler: (route) => fulfillJson(route, [makeTenant()]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/runs",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/secrets",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/status",
      handler: (route) =>
        fulfillJson(route, {
          services: [
            { service_id: "api", label: "API", status: "healthy", summary: "Serving requests", capabilities: [] },
            {
              service_id: "workers",
              label: "Workers",
              status: "healthy",
              summary: "2 workers online",
              capabilities: ["Linux", "macOS"],
              instances: [
                {
                  instance_id: "worker-linux-01",
                  label: "Linux worker",
                  status: "idle",
                  summary: "Idle and ready",
                  updated_at: "2026-03-28T15:30:00.000Z",
                  last_heartbeat_at: "2026-03-28T15:30:00.000Z",
                  capabilities: ["Linux"],
                },
                {
                  instance_id: "worker-mac-01",
                  label: "macOS worker",
                  status: "busy",
                  summary: "Processing a queued run",
                  updated_at: "2026-03-28T15:31:00.000Z",
                  last_heartbeat_at: "2026-03-28T15:31:00.000Z",
                  capabilities: ["macOS"],
                  current_run_id: "run-123",
                },
              ],
            },
            { service_id: "knowledge_jira_sync", label: "Knowledge sync", status: "healthy", summary: "Leader active", capabilities: [] },
            { service_id: "discord_commands", label: "Discord commands", status: "healthy", summary: "Commands synced", capabilities: [] },
          ],
        }),
    },
  ]);

  await page.goto("/dashboard");
  await page.getByRole("link", { name: "Status" }).click();

  await expect(page).toHaveURL(/\/status$/);
  await expect(page.getByRole("heading", { name: "Platform status" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Workers" })).toBeVisible();
  await expect(page.getByText("Worker instances")).toBeVisible();
  await expect(page.getByText("Linux worker")).toBeVisible();
  await expect(page.getByText("macOS worker")).toBeVisible();
  await expect(page.getByText("Idle and ready")).toBeVisible();
  await expect(page.getByText("Processing a queued run")).toBeVisible();
  await expect(page.getByText("Discord commands")).toBeVisible();
});

test("shows a dedicated Agent runtimes page without duplicating platform status sections", async ({ page }) => {
  await seedAdminSession(page);
  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, makePlatformAdminPrincipal()),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants",
      handler: (route) => fulfillJson(route, [makeTenant()]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/runs",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/secrets",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/agent-runtimes",
      handler: (route) =>
        fulfillJson(route, {
          role_routing: {
            engineering: "engineering_execution_default",
            review: "engineering_execution_deep",
          },
          name_routing: {
            workflow_review_default: "engineering_execution_deep",
          },
          selector_routing: {
            "discord.voice_room_pm": "pm_conversation_fast",
            "discord.voice_entry_router": "general_planning",
          },
          available_roles: ["pm", "engineering", "test", "review", "marketing"],
          available_named_agents: ["workflow_review_default", "pm_primary"],
          available_selectors: ["discord.voice_room_pm", "discord.voice_entry_router", "discord.pm_answer"],
          available_profiles: {
            engineering_execution_default: {
              profile_name: "engineering_execution_default",
              runtime_kind: "codex_cli",
              cli_command: "codex",
              model: "gpt-5.4",
              reasoning_effort: "medium",
              tool_bridge_allowed: true,
              fallback_profile: null,
            },
            engineering_execution_deep: {
              profile_name: "engineering_execution_deep",
              runtime_kind: "codex_cli",
              cli_command: "codex",
              model: "gpt-5.4",
              reasoning_effort: "high",
              tool_bridge_allowed: true,
              fallback_profile: null,
            },
            pm_conversation_default: {
              profile_name: "pm_conversation_default",
              runtime_kind: "chat_cli",
              cli_command: "chat",
              model: "gpt-5.4",
              reasoning_effort: "medium",
              tool_bridge_allowed: false,
              fallback_profile: "general_planning_default",
            },
          },
          effective_defaults: {
            role_routing: {
              pm: "pm_conversation_default",
              engineering: "engineering_execution_default",
              test: "engineering_execution_default",
              review: "engineering_execution_default",
              marketing: "pm_conversation_default",
            },
            name_routing: {
              workflow_review_default: "engineering_execution_deep",
              pm_primary: "pm_conversation_default",
            },
            selector_routing: {
              "discord.voice_room_pm": "pm_conversation",
              "discord.voice_entry_router": "general_planning",
            },
          },
        }),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/agent-runtime-profiles",
      handler: (route) =>
        fulfillJson(route, {
          profiles: {
            engineering_execution_default: {
              profile_name: "engineering_execution_default",
              runtime_kind: "codex_cli",
              cli_command: "codex",
              model: "gpt-5.4",
              reasoning_effort: "medium",
              tool_bridge_allowed: true,
              fallback_profile: null,
              base_url: null,
              api_key_secret_ref: null,
              is_builtin: true,
              is_overridden: false,
              can_delete: false,
              can_reset: false,
              usage_references: ["role:engineering"],
            },
            engineering_execution_deep: {
              profile_name: "engineering_execution_deep",
              runtime_kind: "codex_cli",
              cli_command: "codex",
              model: "gpt-5.4",
              reasoning_effort: "high",
              tool_bridge_allowed: true,
              fallback_profile: null,
              base_url: null,
              api_key_secret_ref: null,
              is_builtin: true,
              is_overridden: false,
              can_delete: false,
              can_reset: false,
              usage_references: ["named-agent:workflow_review_default"],
            },
            pm_conversation_default: {
              profile_name: "pm_conversation_default",
              runtime_kind: "chat_cli",
              cli_command: "chat",
              model: "gpt-5.4",
              reasoning_effort: "medium",
              tool_bridge_allowed: false,
              fallback_profile: "general_planning_default",
              base_url: null,
              api_key_secret_ref: null,
              is_builtin: true,
              is_overridden: false,
              can_delete: false,
              can_reset: false,
              usage_references: ["role:pm"],
            },
          },
        }),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/agent-runtime-tools",
      handler: (route) =>
        fulfillJson(route, {
          available_stages: ["pm", "dev", "test", "review", "orchestrator", "decision_planner"],
          tools: [
            {
              tool_name: "repo.read",
              category: "repo",
              description: "Read files and repository metadata using guarded read-only commands.",
              stages: ["pm", "dev", "test", "review", "orchestrator", "decision_planner"],
            },
            {
              tool_name: "jira.comment",
              category: "jira",
              description: "Post a Jira comment on the active issue.",
              stages: ["pm", "dev", "test", "review", "orchestrator"],
            },
            {
              tool_name: "github.open_pr",
              category: "github",
              description: "Open a pull request for the current branch.",
              stages: ["dev", "review", "orchestrator"],
            },
          ],
        }),
    },
  ]);

  await page.goto("/dashboard");
  await page.getByRole("link", { name: "Agent runtimes" }).click();

  await expect(page).toHaveURL(/\/agent-runtimes$/);
  await expect(page.getByRole("heading", { name: "Agent runtimes" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Routing" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Profiles" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Tools", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Runtime routing" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Role defaults" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Execution selectors" })).toBeVisible();
  await expect(
    page
      .getByRole("row")
      .filter({ has: page.getByRole("cell", { name: "discord.voice_entry_router" }) })
      .getByRole("cell", { name: "discord.voice_entry_router" }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "Platform status" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Workers" })).toHaveCount(0);
  await expect(page.getByText("Worker instances")).toHaveCount(0);

  await page.getByRole("button", { name: "Tools", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Implemented tools" })).toBeVisible();
  await expect(page.getByText("repo.read")).toBeVisible();
  await expect(page.getByText("github.open_pr")).toBeVisible();
  await expect(page.getByText("pm, dev, test, review, orchestrator, decision_planner")).toBeVisible();
});

test("covers runtime profile form permutations across every provider on create and update", async ({ page }) => {
  await seedAdminSession(page);

  const profileState: Record<string, Record<string, unknown>> = {
    engineering_execution_default: {
      profile_name: "engineering_execution_default",
      runtime_kind: "codex_cli",
      cli_command: "codex",
      model: "gpt-5.4",
      reasoning_effort: "medium",
      tool_bridge_allowed: true,
      fallback_profile: null,
      base_url: null,
      api_key_secret_ref: null,
      is_builtin: true,
      is_overridden: false,
      can_delete: false,
      can_reset: false,
      usage_references: ["role:engineering"],
    },
  };
  const createdPayloads = new Map<string, Record<string, unknown>>();
  const updatedPayloads = new Map<string, Record<string, unknown>>();

  const listProfilesBody = () => ({ profiles: profileState });

  await installBffApiMocks(page, [
    {
      method: "GET",
      pathname: "/api/bff/api/app/auth/me",
      handler: (route) => fulfillJson(route, makePlatformAdminPrincipal()),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/tenants",
      handler: (route) => fulfillJson(route, [makeTenant()]),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/runs",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/secrets",
      handler: (route) => fulfillJson(route, []),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/agent-runtimes",
      handler: (route) =>
        fulfillJson(route, {
          role_routing: {},
          name_routing: {},
          available_roles: ["engineering"],
          available_named_agents: ["workflow_dev_default"],
          available_profiles: profileState,
          effective_defaults: {
            role_routing: { engineering: "engineering_execution_default" },
            name_routing: { workflow_dev_default: "engineering_execution_default" },
          },
        }),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/agent-runtime-profiles",
      handler: (route) => fulfillJson(route, listProfilesBody()),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/agent-runtime-tools",
      handler: (route) =>
        fulfillJson(route, {
          available_stages: ["pm", "dev", "test", "review", "orchestrator", "decision_planner"],
          tools: [
            {
              tool_name: "repo.read",
              category: "repo",
              description: "Read files and repository metadata using guarded read-only commands.",
              stages: ["pm", "dev", "test", "review", "orchestrator", "decision_planner"],
            },
          ],
        }),
    },
    {
      method: "GET",
      pathname: "/api/bff/api/admin/codex/models",
      handler: (route, url) => fulfillJson(route, makeModelCatalog(url.searchParams.get("runtime_kind") ?? "codex_cli")),
    },
    {
      method: "POST",
      pathname: "/api/bff/api/admin/agent-runtime-profiles",
      handler: async (route) => {
        const payload = (await route.request().postDataJSON()) as Record<string, unknown>;
        const validationError = validateRuntimePayload(payload);
        if (validationError) {
          return fulfillJson(route, { detail: validationError }, 400);
        }
        const profileName = String(payload.profile_name);
        createdPayloads.set(profileName, payload);
        profileState[profileName] = {
          ...payload,
          is_builtin: false,
          is_overridden: true,
          can_delete: true,
          can_reset: false,
          usage_references: [],
        };
        return fulfillJson(route, profileState[profileName]);
      },
    },
    {
      method: "PUT",
      pathname: /\/api\/bff\/api\/admin\/agent-runtime-profiles\/[^/]+$/,
      handler: async (route) => {
        const payload = (await route.request().postDataJSON()) as Record<string, unknown>;
        const profileName = route.request().url().split("/").pop() ?? "unknown";
        const validationError = validateRuntimePayload(payload);
        if (validationError) {
          return fulfillJson(route, { detail: validationError }, 400);
        }
        updatedPayloads.set(profileName, payload);
        profileState[profileName] = {
          ...profileState[profileName],
          ...payload,
          profile_name: profileName,
          is_builtin: Boolean(profileState[profileName]?.is_builtin),
          is_overridden: true,
          can_delete: !profileState[profileName]?.is_builtin,
          can_reset: Boolean(profileState[profileName]?.is_builtin),
        };
        return fulfillJson(route, profileState[profileName]);
      },
    },
  ]);

  await page.goto("/agent-runtimes");
  await page.getByRole("button", { name: "Profiles" }).click();
  await expect(page.getByRole("heading", { name: "Profiles" })).toBeVisible();

  for (const entry of RUNTIME_FORM_MATRIX) {
    const profileName = `${entry.runtimeKind}_profile`;
    const updateCliCommand = entry.expectedCliCommand ? `${entry.expectedCliCommand}-custom` : undefined;
    const updateBaseUrl = entry.expectedBaseUrl ? `${entry.expectedBaseUrl}/custom` : undefined;
    const updateApiKeyRef = entry.requiresApiKey || entry.allowsOptionalApiKey ? `platform/${entry.runtimeKind}_updated_key` : null;

    await page.getByRole("button", { name: "New profile" }).click();
    await page.getByLabel("Profile name").fill(profileName);
    await page.getByLabel("Runtime").selectOption(entry.runtimeKind);

    if (entry.expectedCliCommand) {
      await expect(page.getByLabel("CLI command")).toBeVisible();
      await expect(page.getByLabel("CLI command")).toHaveValue(entry.expectedCliCommand);
    } else {
      await expect(page.getByLabel("CLI command")).toHaveCount(0);
    }

    if (entry.expectedBaseUrl) {
      await expect(page.getByLabel("Base URL")).toBeVisible();
      await expect(page.getByLabel("Base URL")).toHaveValue(entry.expectedBaseUrl);
    } else {
      await expect(page.getByLabel("Base URL")).toHaveCount(0);
    }

    if (entry.requiresApiKey || entry.allowsOptionalApiKey) {
      await expect(page.getByLabel("API key secret ref")).toBeVisible();
    } else {
      await expect(page.getByLabel("API key secret ref")).toHaveCount(0);
    }

    await expect(page.getByLabel("Reasoning mode")).toBeEnabled();

    if (entry.usesPresetModel) {
      await page.getByLabel("Model", { exact: true }).selectOption(entry.createModelValue);
    } else {
      await page.getByLabel("Model", { exact: true }).selectOption({ label: "Custom model…" });
      await page.getByLabel("Model custom value").fill(entry.createModelValue);
    }

    const createButton = page.getByRole("button", { name: "Create profile" });
    if (entry.requiresApiKey) {
      await expect(createButton).toBeDisabled();
      await page.getByLabel("API key secret ref").fill(`platform/${entry.runtimeKind}_api_key`);
      await expect(createButton).toBeEnabled();
    } else {
      await expect(createButton).toBeEnabled();
    }

    await page.getByLabel("Reasoning mode").selectOption("medium");
    await createButton.click();

    await expect.poll(() => createdPayloads.has(profileName)).toBe(true);
    const createdPayload = createdPayloads.get(profileName);
    expect(createdPayload).toBeDefined();
    expect(createdPayload?.runtime_kind).toBe(entry.runtimeKind);
    expect(createdPayload?.model).toBe(entry.createModelValue);
    expect(createdPayload?.reasoning_effort).toBe("medium");
    expect(createdPayload?.cli_command ?? "").toBe(entry.expectedCliCommand ?? "");
    expect(createdPayload?.base_url ?? null).toBe(entry.expectedBaseUrl ?? null);
    if (entry.requiresApiKey) {
      expect(createdPayload?.api_key_secret_ref).toBe(`platform/${entry.runtimeKind}_api_key`);
    } else {
      expect(createdPayload?.api_key_secret_ref ?? null).toBe(null);
    }

    await expect(page.getByLabel("Profile name")).toHaveValue(profileName);
    await expect(page.getByRole("button", { name: "Save profile" })).toBeVisible();

    if (updateCliCommand) {
      await page.getByLabel("CLI command").fill(updateCliCommand);
    }
    if (updateBaseUrl) {
      await page.getByLabel("Base URL").fill(updateBaseUrl);
    }
    if (updateApiKeyRef) {
      await page.getByLabel("API key secret ref").fill(updateApiKeyRef);
    }
    await page.getByLabel("Reasoning mode").selectOption("high");
    if (entry.usesPresetModel) {
      await page.getByLabel("Model", { exact: true }).selectOption(entry.updateModelValue);
    } else {
      await expect(page.getByLabel("Model custom value")).toBeVisible();
      await page.getByLabel("Model custom value").fill(entry.updateModelValue);
    }
    await page.getByRole("button", { name: "Save profile" }).click();

    await expect.poll(() => updatedPayloads.has(profileName)).toBe(true);
    const updatedPayload = updatedPayloads.get(profileName);
    expect(updatedPayload).toBeDefined();
    expect(updatedPayload?.runtime_kind).toBe(entry.runtimeKind);
    expect(updatedPayload?.model).toBe(entry.updateModelValue);
    expect(updatedPayload?.reasoning_effort).toBe("high");
    expect(updatedPayload?.cli_command ?? "").toBe(updateCliCommand ?? "");
    expect(updatedPayload?.base_url ?? null).toBe(updateBaseUrl ?? null);
    expect(updatedPayload?.api_key_secret_ref ?? null).toBe(updateApiKeyRef);

    await page.reload();
    await page.getByRole("button", { name: "Profiles" }).click();
    await page.getByRole("row").filter({ hasText: profileName }).click();
    await expect(page.getByLabel("Profile name")).toHaveValue(profileName);
    await expect(page.getByLabel("Runtime")).toHaveValue(entry.runtimeKind);
    await expect(page.getByLabel("Reasoning mode")).toHaveValue("high");
  }
});

test("redirects unauthenticated access to login for protected routes", async ({ page }) => {
  await page.goto("/tenants/select");

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("keeps the public home page available without redirecting to login", async ({ page }) => {
  await page.goto("/");

  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { name: "Transforming vision into digital reality." })).toBeVisible();
  await expect(page.locator('a[href="/login"]').first()).toBeVisible();
});

test("keeps auth pages linked back to the public home page", async ({ page }) => {
  await page.goto("/login");
  await expect(page.getByRole("link", { name: "Back to home" })).toBeVisible();
  await page.getByRole("link", { name: "Back to home" }).click();
  await expect(page).toHaveURL(/\/$/);

  await page.goto("/register");
  await expect(page.getByRole("link", { name: "Back to home" })).toBeVisible();

  await page.goto("/forgot-password");
  await expect(page.getByRole("link", { name: "Back to home" })).toBeVisible();

  await page.goto("/reset-password?token=sample-token");
  await expect(page.getByRole("link", { name: "Back to home" })).toBeVisible();
});

test("submits the login form and lands on platform admin home", async ({ page }) => {
  await page.goto("/login");

  await page.getByLabel("Email or username").fill("admin");
  await page.getByLabel("Password").fill(process.env.ORCHESTRATOR_ADMIN_PASSWORD ?? "change-me");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/dashboard$/, { timeout: 15000 });
  await expect(page.getByRole("heading", { name: "Operations Overview" })).toBeVisible();
});

test("logging out fully ends the session before another user signs in", async ({ page, request }) => {
  const suffix = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const userTwoEmail = `playwright-auth-two-${suffix}@example.com`;
  const userTwoPassword = "PlaywrightPass456!";
  let tenantId = "";

  try {
    const userTwoResponse = await request.post("http://localhost:4000/api/public/register", {
      data: {
        full_name: "User Two",
        email: userTwoEmail,
        password: userTwoPassword,
        tenant_name: `Auth Workspace Two ${suffix}`,
      },
    });
    expect(userTwoResponse.ok()).toBeTruthy();
    const userTwoRegistration = await userTwoResponse.json();
    tenantId = userTwoRegistration.tenant.tenant_id as string;

    await page.goto("/login");
    await page.getByLabel("Email or username").fill(process.env.ORCHESTRATOR_ADMIN_USERNAME ?? "admin");
    await page.getByLabel("Password").fill(process.env.ORCHESTRATOR_ADMIN_PASSWORD ?? "change-me");
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page).toHaveURL(/\/dashboard$/, { timeout: 15000 });
    await expect(page.getByRole("heading", { name: "Operations Overview" })).toBeVisible();

    await page.getByRole("button", { name: "Logout" }).click();
    await expect(page).toHaveURL(/\/login$/, { timeout: 15000 });

    await page.getByLabel("Email or username").fill(userTwoEmail);
    await page.getByLabel("Password").fill("wrong-password");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/login$/);

    await page.getByLabel("Email or username").fill(userTwoEmail);
    await page.getByLabel("Password").fill(userTwoPassword);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/get-started$/, { timeout: 15000 });
  } finally {
    if (tenantId) {
      await archiveTenant(request, tenantId);
    }
  }
});

test("requests a password reset and completes it through the real browser flow", async ({ page, request }) => {
  const email = `playwright-reset-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
  const oldPassword = "PlaywrightPass123!";
  const newPassword = "PlaywrightPass789!";
  let tenantId = "";

  try {
    const registerResponse = await request.post("http://localhost:4000/api/public/register", {
      data: {
        full_name: "Reset User",
        email,
        password: oldPassword,
        tenant_name: `Reset Workspace ${Date.now()}`,
      },
    });
    expect(registerResponse.ok()).toBeTruthy();
    const registration = await registerResponse.json();
    tenantId = registration.tenant.tenant_id as string;

    await page.goto("/forgot-password");
    await page.getByLabel("Work email").fill(email);
    await page.getByRole("button", { name: "Send reset link" }).click();
    await expect(page.getByText("If an account exists for that email, a reset link has been sent.")).toBeVisible();

    const messageSearch = await request.get(`http://localhost:4206/api/v1/search?query=${encodeURIComponent(email)}`);
    expect(messageSearch.ok()).toBeTruthy();
    const searchPayload = await messageSearch.json();
    expect(Array.isArray(searchPayload.messages)).toBeTruthy();
    const resetMessage = searchPayload.messages.find((message: { Subject?: string }) =>
      String(message.Subject ?? "").includes("Reset your Master Builder password"),
    );
    expect(resetMessage).toBeTruthy();

    const messageResponse = await request.get(`http://localhost:4206/api/v1/message/${resetMessage.ID as string}`);
    expect(messageResponse.ok()).toBeTruthy();
    const messagePayload = await messageResponse.json();
    const textBody = String(messagePayload.Text ?? "");
    const match = textBody.match(/https?:\/\/[^\s]+\/reset-password\?token=[^\s]+/);
    expect(match).toBeTruthy();

    await page.goto(new URL(match![0]).pathname + new URL(match![0]).search);
    await page.getByLabel("New password").fill(newPassword);
    await page.getByRole("button", { name: "Reset password" }).click();
    await expect(page).toHaveURL(/\/login\?reset=success$/, { timeout: 15000 });
    await expect(page.getByText("Password updated. Sign in with your new password.")).toBeVisible();

    await page.getByLabel("Email or username").fill(email);
    await page.getByLabel("Password").fill(oldPassword);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.locator("p[role='alert']")).toContainText("Invalid credentials");

    await page.getByLabel("Password").fill(newPassword);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/get-started$/, { timeout: 15000 });
  } finally {
    if (tenantId) {
      await archiveTenant(request, tenantId);
    }
  }
});
