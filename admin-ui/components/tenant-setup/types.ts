export const STEP_ORDER = [
  { key: "basics", label: "Tenant Basics" },
  { key: "jira", label: "Connect Jira" },
  { key: "github", label: "Connect GitHub App" },
  { key: "discord", label: "Connect Discord Bot" },
  { key: "repos", label: "Repositories + Policy" },
  { key: "review", label: "Review + Save" }
] as const;

export type WizardStepKey = (typeof STEP_ORDER)[number]["key"];
