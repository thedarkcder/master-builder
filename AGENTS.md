# AGENTS

Repository execution policy for Codex terminal sessions.

## Required preflight (before coding)
- Read `.codex/POLICY.md`.
- Read `.codex/ENGINEERING_STANDARDS.md`.
- Read `.codex/OPERATING.md`.
- Apply `.codex/skills/staff-engineer-review/SKILL.md`.
- Apply `.codex/skills/jira-feature-flow/SKILL.md`.
- Read the most relevant language file from `.codex/policy_pack.*.json`.
- If requirements or design/NFR trade-offs are unclear, run a Decision Gate using `.codex/DECISION_GATE_TEMPLATE.md` and stop coding.

## Engineering design expectations
- Prefer composition and small focused modules/classes over "god" files or classes.
- Keep core logic pure/testable; keep IO and side effects at the edges.
- Model multi-step workflows with explicit state models and transitions.
- Do not use sleep/timer-based synchronization in production or tests unless explicitly justified and bounded.

## Quality gates
- Keep diffs scoped to the ticket; avoid unrelated refactors/reformatting.
- Add/update tests for behavior changes.
- Do not leave placeholder/TODO production paths.


## Required code review (after coding)
- Read `.codex/ENGINEERING_STANDARDS.md`.
- Read `.codex/OPERATING.md`.
- Apply `.codex/skills/staff-engineer-review/SKILL.md`.
- Apply `.codex/skills/jira-feature-flow/SKILL.md`.
- Read `.codex/skills/verification-before-completion/SKILL.md`.
- Read the most relevant language file from `.codex/policy_pack.*.json`.
- If the proposed solution does not meet the coding standard, re-engineer your implementation coding.

## Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problens, throw more compute at it via subagents
- One task per subagent for focused execution

## Self-Improvement Loop
- After ANY correction from the user: update 'tasks/lessons.md" with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project
