# Skill: Run Tests

## Goal
Run the correct test suite for the repo, interpret failures, and fix deterministically.

## Steps
1) Identify the correct test command:
   - Prefer `.codex/policy_pack.*.json` -> `test_commands`
   - Else prefer repo scripts (`package.json`, `pom.xml`, CI config)
2) Run tests locally.
3) If failures:
   - Capture the minimal relevant output (no secrets).
   - Fix root cause (not by adding sleeps).
   - Re-run tests until green.
4) Ensure new/changed behavior has tests.

## Rules
- No time-based synchronization to fix flakiness.
- Use condition-based waits, mocks, fake timers, or state machines.
- Keep changes minimal.

## Output
- Record test command used.
- Summarize what failed and what changed.
