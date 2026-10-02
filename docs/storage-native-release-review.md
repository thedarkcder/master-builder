# Storage and native release follow-up

## 0. Reality model

Facts: recordings must remain private and retained for a defined period; native voice
requires a distributable encrypted transport. Maintainers own release decisions and
storage credentials. Upstream projects own their component grants. Events: the
maintainer selected SeaweedFS and OpenSSL 3 and requested a push. Artifacts: builds,
tests and inventories provide evidence, not legal clearance. Product truth is usable
private recordings and voice; the current default storage and native archive do not
establish maintained, distributable implementations. Lifecycle: selected → tested →
installed; existing storage requires copied-and-verified evidence before cutover.
Missing evidence stops the affected transition; duplicate setup must preserve data
and privacy. Root cause: operational/reliability failure. Anti-patching verdict:
✅ Model understood — proceed to Phase 1.

## 0.5 Design search

Retaining archived MinIO builds preserves an unmaintained default. Managed S3
adds an account and cost to local setup. SeaweedFS preserves local Docker and S3
ownership with a maintained community project; the maintainer selected it.
Replacing DAVE changes voice interoperability; OpenSSL 3 is an upstream-supported
build of the existing protocol and was selected instead. The trade-off frontier
is reached: storage operation and native builds still require exact-version proof.
Greenfield would retain private S3 and upstream encrypted transport. Deleting
permission or migration boundaries violates privacy or loses stored evidence.
Choose explicit provider replacement and one native crypto build; reject automatic
fallbacks. Incompatible permissions, retention or linked licenses would invalidate
the chosen implementation and block it rather than permit weakened defaults.

## 1. Problem

External developers need maintained private storage and a verifiable native transport.

## 2. Type

Integration boundary.

## 3. Invariants

- Anonymous callers cannot retrieve recordings; application access is bucket scoped.
- Keep retention explicit and verify it against the actual pinned storage version.
- Only the OpenSSL 3 native path is installed; preserve upstream source and notices.
- Existing data is not discarded or silently switched to an empty replacement.
- Push only reviewed, scanned source to the maintainer's specified destination.

## 4. Assumptions

Scope is the current single-host local Docker setup, not a new production storage
SLO. No live data migration or credential rotation is implied. Source configuration
changes are reversible; operators explicitly migrate existing volumes. This is a
direct maintainer task, not private Jira automation. Push destination/visibility
must be established before external writes.

## 5. Contract matrix

| Input | Expected behavior |
| --- | --- |
| Valid scoped credentials and target bucket | Put/stat/Get succeeds |
| Anonymous, wrong credentials or unrelated bucket | Denied |
| Missing configuration or unsupported retention | Setup fails clearly |
| Existing old storage | Explicit backup/copy/verification/cutover, no deletion |
| Native source/compiler unavailable or linked legacy crypto | Build fails |
| Unknown Git destination | Continue local work; do not push |

## 6. Call-path impact scan

Compose and hybrid worker startup provision storage consumed by the existing S3
client and authenticated artifact routes. Native Docker builders invoke the libdave
installer; Go transport linking and distribution collection consume its outputs.
README, configuration examples and setup tests must describe the same contracts.

## 7. Domain term contracts

"Private" requires anonymous and wrong-scope denial. "Retained" requires configured
expiry and separate operational deletion proof. "OpenSSL 3" requires linked binary
evidence. "Cleared" cannot be inferred from compilation or a declared license.

## 8. Authorization & data-access contract

Keep tenant/project/run authorization and authenticated delivery unchanged. Storage
administration uses separate credentials; app credentials must not administer other
buckets. Source push authorization does not authorize credential disclosure, remote
force-push, publishing a release or deleting operator volumes.

## 9. Lifecycle & state matrix

New storage: configured → healthy → permissions/retention initialized → usable.
Existing storage: backed up → copied → hash verified → explicit cutover. Failures
retain the source and stop cutover. Native: pinned source → build → linked dependency
inspection → notices/material inventory → integration proof. Missing/out-of-order
evidence cannot advance to release clearance; repeated initialization is tested.

## 10. Proposed design

Keep the existing S3 client and delivery interface. Replace only local provisioning,
document explicit data migration, and remove obsolete MinIO defaults/recipes.
Build pinned upstream libdave with OpenSSL 3 and retain verifiable material receipts.
Update README and the readiness report with precise verification limits.

## 11. Patterns used

Explicit configuration, bounded initialization, immutable upstream versions,
verified migration and integration tests at real storage/build boundaries.

## 12. Patterns not used

No dual storage defaults, BoringSSL fallback, new application storage abstraction,
voice protocol rewrite or automatic volume destruction.

## 13. Change surface

Compose, storage setup/migration scripts, hybrid startup, native installer/builders,
their tests and release documentation. Storage replacement needs operator migration;
existing URLs continue through the application's authenticated delivery contract.

## 14. Load shape & query plan

No application queries or authorization fan-out added. Migration is streamed and
bounded per object rather than materializing a bucket into memory. Existing request
body, object-size and timeout bounds remain in effect.

## 15. Failure modes

Reject missing credentials, anonymous access, scope widening, unsupported lifecycle
and mismatched migration bytes. Fail source/build/checksum/link inspection rather
than switching crypto. Report environment failures separately from code failures.

## 16. Operational integrity

Back up and retain the old volume until verified cutover; no production migration
runs during this task. Run only disposable proof containers. Check disk before
builds and coordinate build concurrency; do not prune shared resources. Replaying
setup must preserve objects and denial rules. Remote push must be non-destructive
unless separate explicit history replacement authorization is obtained.

## 17. Tests

Test-first storage/native contracts, actual S3 upload/stat/download and anonymous,
wrong-key and unrelated-bucket denials, retention and initialization replay, migration
failure paths, exact native linking/source/notices, targeted packaging/material tests,
global lint/format and redacted current-tree/history scans. Full-suite and encrypted
voice interoperability limits remain explicit. Existing browser delivery behavior
is retained; this change owns storage and native build boundaries, not new UI flows.

## 18. Verdict

✅ Proceed — design is appropriate and scoped

This permits the authorized local implementation and verified source push once its
destination is known. It does not clear the remaining public-release blockers.

## Post-implementation contract review

The selected source push targets the existing private
`thedarkcder/master-builder` repository on `codex/open-source-readiness`, without
force-pushing or transmitting unrelated refs, tags or stashes. This does not clean
the old remote history or change repository visibility.

Real route-smoke verification exposed an owned public-schema defect: tenant Discord
configuration discarded `command_secret_ref`, preventing the authenticated command
bridge from being configured. The public API now accepts a managed-secret reference,
preserves it on partial updates and explicitly clears it on null; blank values are
rejected. Migration 0137 normalizes historical whitespace/empty references, validates
the whole batch before writing, and fails without logging values on malformed data.
It does not invent a secret or restore an implicit project. API/JSON migration
regressions reproduce configuration, authorization, clear/repeat, validation and
partial-write failure boundaries. There is no UI field for this bridge setting;
its acceptance path is the documented API and callback, not a browser form.

Existing default-project test fixtures now use IDs returned by actual project
creation/listing. Production project lookup has no compatibility shim. Route smoke
coverage still has a synthetic stream shortcut, which is not real streaming proof.
No production database or storage volume was migrated during this review.
