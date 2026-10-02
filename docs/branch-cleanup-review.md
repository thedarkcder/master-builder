# Main promotion and branch cleanup

Review date: 2026-10-02. This is repository consolidation, not public-release clearance.

## 0. Reality model

The maintainer requested that the prepared source become main and old branches be
removed. Thirty remote heads exist. Local history has already been sanitized;
remote historical commits have different IDs. Source and recovery evidence belong
to the maintainer, remote refs belong to GitHub, and remote caches are outside Git
push ownership. State: inventoried → recoverable → main verified → old heads removed
→ local checkout verified. Root cause: obsolete ref/history ownership. Anti-patching
verdict: model understood; consolidate refs without merging removed history back.

## 0.5 Design search

A merge with old remote main would retain unsanitized ancestors. A new orphan root
would discard useful cleaned history. Renaming the default branch leaves old main
reachable. Choose the prepared sanitized history as main with an exact expected-old
lease, then guarded deletion of inventoried heads. The recovery bundle mitigates
loss of unique local work. Unexpected concurrent updates or protection failures
invalidate the operation rather than allowing unguarded force.

## 1. Problem

Give the maintained source one main branch without keeping obsolete remote heads.

## 2. Type

Repository lifecycle boundary.

## 3. Invariants

- Main must resolve to the exact reviewed prepared commit.
- Do not reintroduce unsanitized commits or push tags/stashes/recovery refs.
- Preserve sanitized unique work privately before branch removal.
- Abort if a remote head changed after inventory.
- Keep the repository private and retain all release blockers.

## 4. Assumptions

The explicit maintainer request authorizes main promotion and old-branch deletion,
overriding the default no-merge policy for this task. The maintainer subsequently
explicitly authorized removing all 386 inventoried old tags and releases, including
their four assets. No new release or public visibility is authorized. No PR is required for
this requested ref operation; the failing full-suite gate remains documented.

## 5. Contract matrix

| Condition | Action |
| --- | --- |
| Expected old main and reviewed source | Promote with exact lease; verify SHA |
| Main differs after promotion | Stop before deleting old branches |
| Changed branch/protection refusal | Fail and reinventory; no unguarded overwrite |
| Unique sanitized local work | Keep verified mode-0600 Git recovery bundle |
| Old tags/releases/PR caches | Separate review/removal; no clearance inferred |

## 6. Call-path impact scan

GitHub default main and fresh clones use the promoted tree. Main push starts CI.
Repository workflows have no branch-delete or PR-closed subscription. Installed
GitHub Apps and external integrations were not comprehensively inspected.

## 7. Domain term contracts

Clean branches means a verified ref inventory, not destruction of all cached history.
Preserved means recoverable sanitized local refs, not an exact old-to-new SHA map.

## 8. Authorization and data access

Operate only on the maintainer's private `thedarkcder/master-builder` repository.
Thirty branch names correlate with preserved sanitized local remote-tracking refs;
the transient original SHA map was deleted after sanitation. Do not claim exact
remote-tip equivalence. Twenty-one tips retain 38 distinct branch-only commits,
including seven Dependabot tips. They are preserved rather than silently merged.

## 9. Lifecycle and state matrix

Verified local recovery precedes main replacement. Verified remote main precedes
old-head deletion. Verified remote cleanup precedes local branch removal. Failures
stop at the affected transition; never reset away uncommitted work.

## 10. Proposed design

Promote only HEAD to main; delete the other 29 inventoried remote heads and 386 old
tags with exact leases. Delete only inventoried GitHub release IDs after matching
their tag/asset identity. Check out local main and remove obsolete local heads/tags
after recovery proof. Keep stashes and the private bundle out of push refspecs.

## 11. Patterns used

Explicit inventory, expected-value leases, bounded recovery and state verification.

## 12. Patterns not used

No mirror/all/tag push, merge of old history, automatic public release or PR bypass
claim that the full test suite passed.

## 13. Change surface

Remote/local branch refs, repository automation flag and documentation only. No
application behavior, database state or operator storage changes.

## 14. Load shape and query plan

One small local Git bundle and bounded GitHub/Git ref operations. Do not fetch old
remote tags/history into the sanitized checkout or start resource-heavy builds.

## 15. Failure modes

Backup verification, concurrent writes, branch protection and transport failures
stop progress. Inspect actual remote refs after an ambiguous network result. Retain
the recovery bundle; a successful ref deletion is not proof of cached-data removal.

## 16. Operational integrity

No active extra local worktree was found. The existing Jira release automation flag
was true and was disabled before promotion to prevent scheduled internal Jira writes;
the inspected recent runs were completed. No repository push hooks were configured.
Keep CI/security/Dependabot active; new maintenance branches may legitimately appear.
Other clones need deliberate migration to the rewritten history, not a merge of old
history. Release deletion is explicitly authorized. Sequential API writes are
scheduled at most once per second within the finite inventory to respect GitHub's
documented rate limits; this bounded operation scheduling is not readiness polling.
Do not download or republish uncleared historical release assets.

## 17. Tests

Verify recovery bundle integrity/ref coverage, current-tree cleanliness, secret
scanner canaries and all-local-history scan. Verify remote main SHA, head inventory,
default branch/private visibility, affected PR states and local main after cleanup.
No application behavior changes require new software tests. Main CI runs separately;
the previous incomplete full backend suite remains a release gate.

## 18. Verdict

✅ Proceed — design is appropriate and scoped.
