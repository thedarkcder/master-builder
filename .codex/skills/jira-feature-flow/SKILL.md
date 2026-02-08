---
name: jira-feature-flow
description: Use when the task is to implement or update a feature tied to a Jira issue. Fetch the Jira issue, implement changes, comment progress, and transition the issue to the correct workflow state (Done/Blocked/Testing).
---

> For backlog seeding/issue creation-sync work (non-execution), use `codex-jira-backlog` instead of this skill.

## Preflight dependency check (MANDATORY)
Before starting work, verify:
- Issue status indicates it is ready to work on.
- No unresolved blockers or linked issues that must be completed first.
- Acceptance criteria do not depend on unfinished work elsewhere.

If dependencies or blockers are found:
- Do NOT implement any code.
- Add a Jira comment explaining what is blocking the issue.
- Transition the issue to Blocked.
- Stop execution.


### Dependency signals to check
- Jira issue links (blocks / is blocked by / relates to)
- Mentions in the description or acceptance criteria (e.g. "after <OTHER_ISSUE_KEY>")
- Comments indicating sequencing or pending decisions
- Required contracts, schemas, or APIs not yet implemented


## Inputs (must obtain or ask for)
- ISSUE_KEY (for example `<PROJECT_KEY>-<NUMBER>`)
- Desired outcome: Done | Blocked | Testing
- If "Done": PR/branch info (or commit) and any release notes

## Bug-first rule (MANDATORY for regressions/failures)
When work starts from a failing CI run, runtime error, or review-reported defect and there is no Jira key yet:
1) Create a Jira **Bug** issue first (in the correct project).
2) Use the Bug key as the execution ISSUE_KEY.
3) Create/switch branch using that key before any code changes:
   - `jira/<BUG_KEY>-<short-slug>`
4) Link PR title/body to the Bug key and keep all updates on that branch.

If Jira is unavailable/auth fails:
- Stop implementation changes.
- Report blocked state and request Jira access restoration.

## Workflow (follow strictly)
1) Fetch the Jira issue details (title, description, acceptance criteria, current status).
2) Confirm you are working on exactly this ISSUE_KEY for the entire task.
3) Ensure branch name is keyed to ISSUE_KEY:
   - `jira/<ISSUE_KEY>-<short-slug>`
4) Add a Jira comment: "Starting work" + a 1-2 line plan + any assumptions.
5) Implement the work in the repo:
   - Keep changes minimal and aligned to acceptance criteria.
   - Run relevant tests/commands.
6) Add a Jira comment: what changed + how to validate (commands, URLs, steps).
7) Transition the issue:
   - If outcome = Done -> transition to Done
   - If outcome = Testing -> transition to Testing (or your team's equivalent)
   - If outcome = Blocked -> transition to Blocked AND add a comment explaining the blocker + what's needed
8) Final Jira comment: summary + links (PR, commit, build) if available.
9) When creating or editing PR descriptions, use proper Markdown formatting:
   - Use real line breaks and headings/lists (not escaped `\n` text).
   - Prefer `gh pr create --body-file <file>` / `gh pr edit --body-file <file>`.
   - Verify rendering with `gh pr view <number> --json body`.

## Status mapping (edit to match our Jira)
- Testing = "Testing"
- Done = "Done"
- Blocked = "Blocked"
