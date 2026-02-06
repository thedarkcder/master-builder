# Skill: PR Checklist

Before opening PR, confirm:

## Scope and quality
- [ ] Diff is minimal and on-topic
- [ ] No unrelated formatting/refactors
- [ ] No placeholders/TODO stubs in production paths
- [ ] No sleeps/timers used for synchronization

## Tests
- [ ] Tests added/updated for behavior changes
- [ ] Test suite run locally (or explain why not possible)

## Documentation
- [ ] PR description includes summary, risks, how to test
- [ ] Jira comment includes PR link and acceptance criteria status

## Decision Gate
- [ ] If NFR/design trade-offs exist, Decision Gate resolved before coding
