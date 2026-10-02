# Release-readiness design review

## 0. Reality model

Facts: this is an internal, multi-tenant automation project with provider integrations.
Intent: publish a usable AGPL-only release without exposing private material.
Owner: maintainers own publication and legal authority; providers own their terms.
Evidence: tracked files, local history, manifests, regression tests and build results.
Artifacts: reports and generated checks are evidence, not grants of rights.
State: inspected → remediated → verified → maintainer-cleared → published.
Only maintainers may clear rights/history blockers; publication is outside this task.
Duplicate checks do not authorize publication; absent rights evidence blocks it.
Root cause: operational/reliability failure (missing publication controls).
Anti-patching verdict: ✅ Model understood — proceed to Phase 1.

## 0.5 Design search

Candidates: wholesale rewrite; documentation-only release; targeted remediation with
explicit release blockers. The rewrite adds risk without proving ownership; docs
alone leave known security defects. Targeted remediation dominates for scope,
testability and reversibility. Greenfield would use the same explicit clearance
boundary. Deleting the clearance boundary would confuse passing tests with release
permission. No additional lifecycle or compatibility layer is introduced.
Trade-off: unresolved deployment/legal choices remain visible blockers. Verified
license incompatibility or unowned code would stop relicensing affected work.

## 1. Problem

External contributors cannot currently establish a safe, reproducible starting point.

## 2. Type

Reliability / correctness concern.

## 3. Invariants

- Preserve third-party rights and existing human edits.
- Never publish secret values or private runtime artifacts.
- Missing required security configuration fails clearly.
- Passing checks cannot substitute for ownership or publication approval.

## 4. Assumptions

The detailed user request authorizes reversible release maintenance without a Jira
ticket; Jira feature execution rules were reviewed and no ticket execution is claimed.
The maintainer subsequently authorized local sensitive-history cleanup and removal
of private Maven assumptions. See the scoped follow-up review in
[private-material-removal-review.md](private-material-removal-review.md). No remote
force-push, external message, release or live deployment change is authorized.

## 5. Contract matrix

| Input | Expected result |
| --- | --- |
| Valid configured signing key/canonical origin | Existing authentication/link flow |
| Missing or weak signing key | Explicit configuration failure |
| Forged forwarding host | Cannot redirect security link |
| Third-party license | Preserved; not replaced by first-party declaration |
| Unverified rights/history | Release remains blocked |

## 6. Call-path impact scan

Settings feed API, CLI and workers; URL helpers feed security links. Compose/env
examples, native contributor setup and CI must supply the same required settings.

## 7. Domain term contracts

"Licensed" means declared first-party terms, not proven ownership. "Verified" means
the recorded check passed. "Ready" requires independent maintainer clearance.

## 8. Authorization & data-access contract

Review is local and read-only externally. No tenant scope changes or secret disclosure.
Only configured origins may receive authentication links.

## 9. Lifecycle & state matrix

Inspected/remediated/verified remain unpublished. Missing evidence is blocked;
duplicate/out-of-order checks never advance to published.

## 10. Proposed design

Keep first-party declarations in root LICENSE and manifests; document provider
contracts separately. Fix security boundaries with tests; harden local examples,
image contexts and public CI. Keep a report with outstanding evidence requirements.

## 11. Patterns used

Explicit configuration, canonical origin, environment secrets and regression tests.

## 12. Patterns not used

No compatibility shims, runtime rewrites, invented authentication bypasses or CLA.

## 13. Change surface

Configuration/link helpers, examples, repository docs/metadata, CI and release hygiene.
Signing-key changes require rotation/session invalidation; encrypted data needs a
separate controlled re-encryption migration if the published key was used.

## 14. Load shape & query plan

No new queries/fan-out. Configuration checks run during initialization.

## 15. Failure modes

Bad configuration stops startup. Legal/history ambiguity blocks publication.
Baseline quality failures stay explicit; no skipped check is reported as passing.

## 16. Operational integrity

Source edits are reviewable. Local history cleanup is now authorized; live credential
rotation and remote history replacement remain maintainer actions.
No new concurrency/retry behavior. Provider failures retain explicit diagnostics.

## 17. Tests

Regression tests for signing configuration and link origins; full pytest, Ruff,
format checks, UI lint/build/E2E, wheel/sdist build, metadata checks, dependency audits,
current-tree and local-history secret/reference scans.

## 18. Verdict

⚠️ Proceed with constraints

Post-implementation review: the scoped security/configuration/licensing fixes have
regression evidence, and the mocked UI suite passes. Publication remains blocked
by remote/history-copy cleanup, deployed key review, worker isolation and incomplete
backend verification. Local history and private Maven removal were completed under
the subsequent explicit authorization. Those contracts are not changed by guessing a replacement, restoring
default-project fallbacks or treating a passing subset as full acceptance.

The permitted next steps are bounded verification, documentation and the explicit
maintainer decisions in `docs/open-source-decision-gates.md`. No commit, publication,
remote history replacement, live key migration or unsupported capability claim is authorized
by this verdict. See `OPEN_SOURCE_READINESS_REPORT.md` for final check outcomes.
