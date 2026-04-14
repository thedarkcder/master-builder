export type WizardStepDefinition = {
  key: string;
  label: string;
  title: string;
  description: string;
};

export const STEP_ORDER = [
  {
    key: "basics",
    label: "Basics",
    title: "Name the workspace",
    description: "Choose the workspace name. The workspace ID is generated automatically.",
  },
  {
    key: "jira",
    label: "Jira",
    title: "Connect Jira",
    description: "Connect Jira and choose the projects for this workspace.",
  },
  {
    key: "github",
    label: "GitHub",
    title: "Install GitHub",
    description: "Install the GitHub App and load repositories.",
  },
  {
    key: "repos",
    label: "Repository",
    title: "Choose repository defaults",
    description: "Choose the workspace default repository and set run limits.",
  },
  {
    key: "discord",
    label: "Discord",
    title: "Install Discord (optional)",
    description: "Optionally install the bot now, or skip and configure Discord later.",
  },
  {
    key: "invite",
    label: "People",
    title: "Invite users",
    description: "Invite teammates to this workspace now, or skip and do it later.",
  },
  {
    key: "review",
    label: "Review",
    title: "Review setup",
    description: "Confirm the setup and save the workspace.",
  }
] as const satisfies readonly WizardStepDefinition[];

export type WizardStepKey = (typeof STEP_ORDER)[number]["key"];
export const WIZARD_STEP_KEYS = STEP_ORDER.map((step) => step.key) as readonly WizardStepKey[];

const WIZARD_STEP_KEY_SET: ReadonlySet<string> = new Set(WIZARD_STEP_KEYS);

export function isWizardStepKey(value: string): value is WizardStepKey {
  return WIZARD_STEP_KEY_SET.has(value);
}

export function stepIndexForKey(stepKey: WizardStepKey): number {
  return WIZARD_STEP_KEYS.indexOf(stepKey);
}

export function stepKeyAtIndex(index: number): WizardStepKey | null {
  return WIZARD_STEP_KEYS[index] ?? null;
}
