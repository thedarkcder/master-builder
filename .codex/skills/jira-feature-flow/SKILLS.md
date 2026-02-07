---
name: jira-feature-flow
description: Use when the task is to implement or update a feature tied to a Jira issue. Fetch the Jira issue, implement changes, comment progress, and transition the issue to the correct workflow state (Done/Blocked/Testing).
---

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
- Mentions in the description or acceptance criteria (e.g. "after ABC-122")
- Comments indicating sequencing or pending decisions
- Required contracts, schemas, or APIs not yet implemented


## Inputs (must obtain or ask for)
- ISSUE_KEY (e.g. ABC-123)
- Desired outcome: Done | Blocked | Testing
- If "Done": PR/branch info (or commit) and any release notes

## Workflow (follow strictly)
1) Fetch the Jira issue details (title, description, acceptance criteria, current status).
2) Confirm you are working on exactly this ISSUE_KEY for the entire task.
3) Add a Jira comment: "Starting work" + a 1-2 line plan + any assumptions.
4) Implement the work in the repo:
   - Keep changes minimal and aligned to acceptance criteria.
   - Run relevant tests/commands.
5) Add a Jira comment: what changed + how to validate (commands, URLs, steps).
6) Transition the issue:
   - If outcome = Done -> transition to Done
   - If outcome = Testing -> transition to Testing (or your team's equivalent)
   - If outcome = Blocked -> transition to Blocked AND add a comment explaining the blocker + what's needed
7) Final Jira comment: summary + links (PR, commit, build) if available.
8) When creating or editing PR descriptions, use proper Markdown formatting:
   - Use real line breaks and headings/lists (not escaped `\n` text).
   - Prefer `gh pr create --body-file <file>` / `gh pr edit --body-file <file>`.
   - Verify rendering with `gh pr view <number> --json body`.

## Status mapping (edit to match our Jira)
- Testing = "Testing"
- Done = "Done"
- Blocked = "Blocked"
