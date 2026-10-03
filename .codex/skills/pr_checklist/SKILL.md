---
name: pr_checklist
description: Checklist to validate scope, tests, documentation, and decision-gate readiness before opening a PR.
---

# Skill: PR Checklist

Before opening PR, confirm:

## Scope and quality
- [ ] Diff is minimal and on-topic
- [ ] No unrelated formatting/refactors
- [ ] Latest `staging` (stage) is merged into the branch and conflicts are resolved
- [ ] Latest `main` (main) is merged into the staging branch and conflicts are resolved
- [ ] No placeholders/TODO stubs in production paths
- [ ] No sleeps/timers used for synchronization

## Tests
- [ ] Tests added/updated for behavior changes
- [ ] Test suite run locally (or explain why not possible)

## Documentation
- [ ] PR description includes summary, risks, how to test
- [ ] The linked GitHub issue includes the PR link and acceptance criteria status for Master Builder development; tenant execution uses its configured target tracker

## Decision Gate
- [ ] If NFR/design trade-offs exist, Decision Gate resolved before coding
