---
name: jira-feature-flow
description: Use when the task is to implement or update a feature tied to a Jira issue. Fetch the Jira issue, implement changes, comment progress, and transition the issue to the correct workflow state (Done/Blocked/Testing).
---

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

## Status mapping (edit to match our Jira)
- Testing = "Testing"
- Done = "Done"
- Blocked = "Blocked"
