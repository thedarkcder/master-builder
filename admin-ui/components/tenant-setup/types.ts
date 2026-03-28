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
    key: "discord",
    label: "Discord",
    title: "Install Discord",
    description: "Install the bot and confirm the member invite settings.",
  },
  {
    key: "repos",
    label: "Repository",
    title: "Choose repository",
    description: "Choose the primary repository and set run limits.",
  },
  {
    key: "review",
    label: "Review",
    title: "Review setup",
    description: "Confirm the setup and save the workspace.",
  }
] as const;

export type WizardStepKey = (typeof STEP_ORDER)[number]["key"];
