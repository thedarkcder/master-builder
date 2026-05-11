export type BlogPost = {
  slug: string;
  title: string;
  summary: string;
  body: string[];
};

export const blogPosts: BlogPost[] = [
  {
    slug: "ai-scales-code-faster-than-organisations-scale-control",
    title: "AI scales code faster than organisations scale control",
    summary: "Why output increased before governance, review, and shared context caught up.",
    body: [
      "AI coding tools have changed the speed of implementation. A feature request that once waited for an engineer can now become a branch, a diff, and a pull request in minutes.",
      "The organisational systems around that work have not accelerated at the same rate. Product clarification, architectural judgement, standards, review, testing, approval, and auditability still need shared context.",
      "The practical question for enterprise teams is no longer whether AI can produce code. It is whether the organisation can govern the path from intent to merge with enough evidence to trust the result.",
    ],
  },
  {
    slug: "the-bottleneck-moved-from-writing-code-to-trusting-it",
    title: "The bottleneck moved from writing code to trusting it",
    summary: "How engineering leaders should think about validation, quality, and safety as agents improve.",
    body: [
      "As coding agents improve, teams will spend proportionally less time on generation and more time on validation. The review burden moves upstream into intent and downstream into evidence.",
      "That changes the operating model. Teams need to know what the agent understood, what context it used, what standards were applied, which checks ran, and why a reviewer should trust the work.",
      "Trust becomes a delivery discipline, not a feeling. It has to be designed into the workflow before output volume increases.",
    ],
  },
  {
    slug: "why-ai-makes-product-ambiguity-more-expensive",
    title: "Why AI makes product ambiguity more expensive",
    summary: "Unclear requirements now become implementation quickly, pushing product decisions into review.",
    body: [
      "AI compresses the time between an unclear request and a concrete implementation. That compression can hide ambiguity until the code is already written.",
      "When a coding agent fills in missing product decisions, reviewers are forced to reverse-engineer intent from the diff. Product teams lose visibility into assumptions, and engineers spend review time correcting decisions that should have been clarified earlier.",
      "Enterprise AI delivery needs a clarification layer before execution. Otherwise, faster code simply creates faster rework.",
    ],
  },
  {
    slug: "vibe-coding-works-for-individuals-enterprises-need-delivery-systems",
    title: "Vibe coding works for individuals. Enterprises need delivery systems.",
    summary: "Why team-scale AI delivery needs shared workflows, standards, and review gates.",
    body: [
      "Individual AI coding works because one person can hold the whole loop in their head: intent, constraints, implementation, review, and risk.",
      "Enterprise delivery is different. The work passes through product, engineering, security, platform, QA, and release ownership. Private agent sessions do not create the shared memory those teams need.",
      "Scaling AI-assisted engineering responsibly means turning individual supervision into a governed delivery system.",
    ],
  },
  {
    slug: "agent-platforms-manage-agents-software-organisations-need-delivery-governance",
    title: "Agent platforms manage agents. Software organisations need delivery governance.",
    summary: "Why accountable software delivery needs more than agent orchestration.",
    body: [
      "Running agents is not the same as governing software delivery. Agent orchestration can coordinate execution, but enterprise teams still need traceability from intent through review and approval.",
      "The delivery system has to connect Jira context, standards, agent output, evidence, GitHub pull requests, and human decisions. Otherwise the organisation gains activity without accountability.",
      "The missing category is delivery governance: the operating layer that makes AI-generated software reviewable, auditable, and safe to merge.",
    ],
  },
  {
    slug: "what-ctos-should-ask-before-increasing-ai-generated-code-output",
    title: "What CTOs should ask before increasing AI-generated code output",
    summary: "The questions leaders should ask before expanding AI-assisted engineering.",
    body: [
      "The first leadership question should not be how much more code agents can write. It should be how the organisation will know that code is aligned, safe, correct, and approved.",
      "Useful questions include: what intent did the agent work from, what standards were enforced, what evidence was captured, who reviewed the assumptions, and what approval gate controls merge readiness?",
      "Increasing output without those answers turns AI adoption into review debt. Increasing output with governance turns it into a controlled delivery advantage.",
    ],
  },
];

export function getBlogPost(slug: string): BlogPost | undefined {
  return blogPosts.find((post) => post.slug === slug);
}
