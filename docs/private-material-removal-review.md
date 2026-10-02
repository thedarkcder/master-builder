# Sensitive-material removal review

## 0. Reality model

Fact: the working tree contains release maintenance and unrelated human edits.
Fact: local refs, stashes, backup refs and reflogs retain sensitive historical blobs.
Decision: the maintainer explicitly authorized deletion of sensitive Git material and
the private Maven integration. This supersedes the previous pending removal gate.
Ownership: Git cleanup is local; hosted remotes, forks, artifacts and live credential
revocation require their own operations. No publication is implied.
State: inventoried → sanitized scratch history → validated → installed locally →
rescanned and temporary private copies deleted.
Root cause: internal operational material was embedded in source and Git history.
Anti-patching verdict: ✅ Model understood — proceed to Phase 1.

## 0.5 Design search

Delete all Git metadata; redact only HEAD; or rewrite all local history in a restricted
scratch mirror and preserve working changes. Deleting all metadata would lose unrelated
branches/tags/stashes. HEAD-only edits leave old disclosure paths. A validated rewrite
preserves the useful source graph while removing identified sensitive content.
Stop after this candidate dominates scope, reversibility during validation and privacy.
A greenfield publication would exclude the same material; no compatibility layer is
needed. Failure of preservation checks prevents installation.

## 1. Problem

Remove confidential material and private deployment assumptions without losing unrelated work.

## 2. Type

Security and privacy correctness.

## 3. Invariants

- Never print or publish secret values or private identifiers.
- Preserve unrelated current source, staged changes, branches and stash work.
- Preserve required third-party licenses and copyright notices.
- No old sensitive objects survive in local refs, reflogs or object storage.
- No remote write, live credential migration or publication.

## 4. Assumptions

The user's explicit instruction authorizes destructive cleanup of identified sensitive
material. It does not authorize deleting unrelated stash work or a user's external
runtime installations. Scratch history and replacement mappings remain private and
are deleted after verification; no persistent raw-history backup is created.

## 5. Contract matrix

| Input | Result |
| --- | --- |
| Sensitive historical path/blob/identity | Removed or replaced with obvious sanitized metadata |
| Unrelated file/history/stash work | Preserved |
| Required upstream copyright/license | Preserved |
| Generated private Maven settings with proven origin | Removed/migrated |
| Ambiguous persisted settings | Explicit migration error; no guessed deletion |
| Remote copies | Unchanged, separately flagged |

## 6. Call-path impact scan

Git objects feed every branch/tag/stash and tool ref; indexes feed pending user work.
Maven normalization feeds plan/Compose persistence, analyses and deployment releases.

## 7. Domain term contracts

Clean local Git means checked local refs/reflogs/objects; it does not mean erased remote
history, revoked credentials, or physical secure erasure of previously stored bytes.
Generic Maven support means building the selected reactor jar with explicit project
configuration, not injecting company-specific Spring/service defaults.

## 8. Authorization & data-access contract

Operate within this repository and task-owned scratch directories. No remote pushes,
credential disclosure or changes to unrelated hosts. Inspect values privately; report
counts and affected paths only.

## 9. Lifecycle & state matrix

Failure during scratch rewrite leaves the original repository intact. Install only
after scans and preservation checks. Rebuild index against rewritten HEAD while
retaining current unstaged/staged intent. Remove obsolete raw refs/objects and scratch
copies only when validation confirms the replacement.

## 10. Proposed design

Use modern Git and a verified isolated filter tool; sanitize a mirror, including all
stash entries. Restore sanitized refs and objects without overwriting the working tree.
Remove private Maven injections and add a provenance-based Alembic migration.

## 11. Patterns used

Explicit allowlisted cleanup scope, fail-closed validation, deterministic provenance,
and restricted scratch artifacts.

## 12. Patterns not used

No history reset, compatibility adapter, implicit profile, fake credential or force push.

## 13. Change surface

Git metadata/history; Maven planner, regression tests, migration and release docs.
Commit/tag hashes and signatures change during rewriting. Old hashes become invalid.

## 14. Load shape & query plan

History work is bounded by the locally inventoried graph. Migration must preflight
provenance and preserve explicit operator values; no live database is connected here.

## 15. Failure modes

Insufficient disk, mismatched source/index preservation, unclassified sensitive blobs
or ambiguous migration provenance fail clearly before destructive installation.

## 16. Operational integrity

One Git mutation owner. Source workers may edit the working tree but not refs/index.
Modern Git avoids unsupported index artifacts. Scratch snapshots are bounded and
deleted; private live configuration is excluded from the publication candidate.

## 17. Tests

Dry-run miniature history/stash preservation; all-ref/object scans; source/index hashes
before/after; red-before-green Maven and migration regressions; lint/quality/build.
Record exact outcomes and remaining remote/history/credential limits in the report.

## 17.1 Independent Maven and migration review

The reviewed Maven generator retains the generic reactor build and requires
exactly one runtime JAR. It injects no application properties, logging file,
service credentials, profile or database role initialization. Application
configuration and explicit service dependencies remain project-owned.

Migration `20261002_0134` uses captured original planner results and matching
generated artifacts. Evidence is scoped by tenant and project; update keys are
globally unique primary keys. It visits project/app configurations, deployment
snapshots and analysis results within the Alembic transaction. Later explicit
edits are retained; ambiguous or modified generated builds stop with a clear
provenance error. An original user-owned artifact with the same marker is not
silently deleted. Resource alias normalization and valid YAML extension values
are preserved.

Independent review found and reproduced three gaps: paired Compose fields could
be cleaned only partially, unrelated malformed provenance could stop migration,
and valid YAML dates failed JSON fingerprint serialization. These are corrected
and covered by regressions. The final guard also rejects a retained role script
when later build arguments prevent exact artifact restoration. No remaining
actionable issue was found in the reviewed change.

Verification: `.venv/bin/python -m pytest tests/test_maven_configuration_migration.py -q`
passed **13 tests** independently. A read-only `docker compose build --print`
check confirmed that the generated inline Dockerfile's doubled dollars become
shell command substitutions in the actual Bake definition. Executing only its
JAR-selection command against isolated synthetic files rejected zero and two
JARs and copied the single JAR. No image build or live database was used for
these independent checks.

The earlier four-domain reference list leaves one explicit example fixture in
five occurrences across three test files. These matches are generic examples;
no private labels or credential values were disclosed in this review. Reviewed
cleanup retains GNU license text and third-party copyright/license notices.

This review supports the Maven removal and migration design. It does not verify
production database contents, existing deployed services, complete repository
privacy, hosted copies, credential revocation or completed Git object cleanup.
Record the actual Git preservation/removal checks separately before making
those claims.

Post-install local Git verification: all 466 original refs, eight stashes, 1,655
commits and the symbolic alias survived. All 23,195 objects match the validated
candidate. Private-pattern/static-literal checks and fsck pass; working-file byte/mode
checks show no changes. Required legal blobs and all parent edges are preserved.
Raw temporary mirrors and callback artifacts were deleted. Remote cleanup and
live key rotation/re-encryption were not performed.

Root final verification: 128 Maven/migration/Alembic/CLI tests pass, along with
Ruff, scoped formatting, quality gates and fresh distribution builds. The global
format baseline and previously reported full-suite failures remain release blockers.

Final CI scanner review: exact rule/file/source exceptions permit reviewed non-secret
fixtures and identifiers. Current candidate and all-ref/reflog scans pass with zero
findings; injected fresh Fernet, RSA and Discord credentials remain detected. A pinned,
checksum-verified MIT CLI removes the action-license prerequisite. Hosted execution
is unverified; local TOML/YAML/Bash checks pass.

## 18. Verdict

✅ Proceed — design is appropriate and scoped
