---
name: tests-flow
description: Workflow for adding or updating tests, running the right suite, and fixing failures deterministically.
---

# Skill: Tests Flow

## Add/Update Tests

### Goal
Ensure behavior changes are covered by tests.

### Approach
- Prefer unit tests for logic.
- Use integration tests when behavior spans components/services.
- For React, use React Testing Library and test user-visible behavior.

### React specifics
- Use `getByRole` / `findByRole` and `waitFor` for async.
- Never use `setTimeout`/sleep to wait for UI state.
- Mock network calls using MSW if present.

### Output
- List new/updated tests.
- Explain what behavior is covered.

## Run Tests

### Goal
Run the correct test suite for the repo, interpret failures, and fix deterministically.

### Steps
1) Identify the correct test command:
   - Prefer `.codex/policy_pack.*.json` -> `test_commands`
   - Else prefer repo scripts (`package.json`, `pom.xml`, CI config)
2) Define impacted surface:
   - List changed modules/packages/apps from the diff.
   - Map each changed area to at least one targeted test command.
3) Run both categories:
   - Targeted tests for changed behavior.
   - Relevant full suite for the stack/repo (from policy pack or CI command).
4) If failures:
   - Capture the minimal relevant output (no secrets).
   - Fix root cause (not by adding sleeps).
   - Re-run tests until green.
5) Ensure new/changed behavior has tests.

### Hard execution gate (non-optional)
- Do not commit if any required targeted test command failed, was skipped, or was not run.
- Do not open/update PR if the relevant full suite failed, was skipped, or was not run.
- Do not report "tests passed" unless both targeted and relevant full suite are green.
- If tests cannot run (env/tooling blocker), stop, mark blocked, and report exact blocker.

### Rules
- No time-based synchronization to fix flakiness.
- Use condition-based waits, mocks, fake timers, or state machines.
- Keep changes minimal.

### Required report output (must be explicit)
- Commands run:
  - Targeted: `<exact command(s)>`
  - Full suite: `<exact command(s)>`
- Results:
  - Targeted: pass/fail + key counts (or "no tests found" with reason)
  - Full suite: pass/fail + key counts
- Coverage statement:
  - Which changed behavior each targeted command validates
- If blocked:
  - Exact blocker, attempted commands, and what is needed to unblock
