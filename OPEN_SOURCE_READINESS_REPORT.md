# Open-Source Readiness Report

Review date: **2026-10-02**. **Public release remains blocked.** This report covers
current source, configuration, tests, documentation, deployment recipes, imported
material and locally available Git history. Passing checks are evidence for their
stated boundaries, not exhaustive security, confidentiality or legal clearance.

The maintainer confirmed organizational redistribution/relicensing rights, selected
SeaweedFS and OpenSSL 3, and requested that the accumulated work be pushed, promoted
to main and old branches removed. The existing repository is **private**:
[thedarkcder/master-builder](https://github.com/thedarkcder/master-builder).
The reviewed source was first pushed on `codex/open-source-readiness`; the authorized
consolidation promotes that sanitized history directly to main and removes old
heads rather than merging unsanitized ancestry back. See the
[branch review](docs/branch-cleanup-review.md). This does not publish a release or
clear the gates below. Original local private configuration
and maintainer edits were preserved. No production migration, credential rotation
or external message was performed.

## Blockers — must be resolved before publishing

1. **Remote history and deployed credentials.** Local sanitation removed identified
   sensitive values, generated private artifacts, private Maven assignments and
   personal commit metadata, preserving 466 refs, eight stashes and 1,655 commits.
   Branch consolidation replaces remote main and removes inventoried old branch
   heads. It does not clean PR/cache refs, forks, backups, releases or CI artifacts.
   **386 old remote tags and 386 releases, including four assets,** were inventoried
   and still require separate cleanup/review. Coordinate that cleanup and determine which credentials were
   deployed; revoke used credentials and rehearse encryption-key rotation and
   re-encryption. Deleting history does not revoke credentials. Rewritten commits
   invalidate old signatures. Twenty-one sanitized historical branch tips retain
   38 distinct commits outside the prepared source; a verified private local recovery
   bundle preserves those refs. Unrelated refs, tags and stashes must not be mirrored.
2. **Worker isolation.** Repository tools can invoke subprocesses with ambient
   credentials and host filesystem privileges. Read-only command validation does
   not establish filesystem, network or resource isolation. Enforce the tenant
   threat model or exclude unsafe capabilities. An agent CLI sandbox option is not
   proof of orchestrator isolation. See [decision gates](docs/open-source-decision-gates.md).
3. **Full backend verification.** The final bounded full-suite run reached **262
   passed, 1 failed** before CPython 3.11.7 raised `SystemError: AST constructor
   recursion depth mismatch` in an architecture test's `ast.parse`. No architecture
   assertion was reached. Earlier implicit-project and webhook fixture failures
   have been corrected without restoring production fallbacks. Complete the full
   suite on a verified toolchain; the remaining suite has not been established as
   passing. The unchanged failing test passed independently (1/1). This is consistent
   with the parser race corrected in [Python 3.11.8](https://docs.python.org/release/3.11.8/whatsnew/changelog.html),
   but that is an inference rather than a proven root cause. SQLite
   PostgreSQL-listener errors also remain visible in test logs.
4. **Artifact redistribution clearance.** Exact final image/browser/CLI/native
   notices, corresponding source and required relinking material remain incomplete.
   Source manifests and collection tools are not clearance. Android SDK, CUDA,
   model files and voice samples require specific rights review. Optional Python
   Torch/setuptools advisories and native Go advisory reachability remain open.
   GitHub's push response reports **82 dependency alerts on the existing default
   branch** (14 critical, 38 high, 26 moderate, 4 low). Reconcile those alerts with
   this candidate's updated locks and experimental dependencies before publication;
   the server's default-branch count is not a fresh audit of this preparation branch.
   Exclude uncleared capabilities/artifacts from release.
5. **Final native and core image verification.** The OpenSSL 3 library and actual
   CGo transport built and linked on Linux arm64, and 18 native Go tests passed.
   The final Docker recipe's added mandatory Go-test gate/export, complete voice
   runtime, amd64 and live Discord audio are unverified. Host storage pressure
   interrupted image export. The core API image export/startup and UI image are
   likewise incomplete; source API/UI startup is separate evidence.
6. **Private reporting and publication material.** Configure monitored private
   vulnerability and Code of Conduct channels and a supported-version/response
   policy. GitHub private reporting has not been verified as enabled. Publish a
   reviewed commit/archive, not the working directory or local Git configuration.
   Ignored env/authentication/customer workspaces remain private. Remote-only
   content, deployed secret use and every historical binary could not be inspected.

## Important — selected recommendations and remaining operational work

| Recommendation | Implemented and verified | Remaining limit |
| --- | --- | --- |
| External setup | Frozen public Python/npm installs and no-overwrite secret initialization passed in a fresh source snapshot. Actual PostgreSQL provisioning, owner migration through 0136, runtime grants and API health passed; six real browser journeys passed with no retries. Container UI has an explicit internal backend origin. | Final source now includes 0137; fresh PostgreSQL head migration/RLS checks pass, but complete container startup still needs proof. Other platforms/providers remain experimental. |
| Admission and isolation | Registration disabled by default; atomic database-backed login/reset/registration budgets; canonical origins/exact hosts; proxy headers untrusted by default. Non-owning runtime roles, forced RLS and session identity lifecycle tests pass on real PostgreSQL. | Review production TLS, ingress ownership, quotas, database operations and adversarial load. Budgets are not comprehensive anti-abuse controls. |
| Private QA artifacts | Authenticated delivery checks tenant/project/run, path, size, timeout and byte ranges. Migration 0136 repairs only proven old links and invalidates ambiguous evidence for recapture. SeaweedFS 4.48 uses a digest-pinned published image, isolated backend, scoped Get/Put credentials and a 30-day lifecycle policy. | Verify physical deletion, production TLS/IAM, backup retention, legal holds and recovery. Upstream proxies must redact callback tokens independently. |
| Existing storage | Explicit CLI streams bounded copies, verifies SHA-256/content metadata, preserves the source and refuses mismatched destination data. Versioned buckets and tagged objects are rejected rather than silently losing state. | Back up and quiesce writers before migration; retain the old volume until cutover is verified. No user storage was migrated during this review. |
| Formatting | A focused baseline pass formatted 932 of 1,100 Python files with zero final raw AST changes; statement-scoped grouping suppression preserves two original `del` ASTs. Global checks are enforced. | Review semantic changes separately from mechanical formatting; the accumulated push is necessarily large. |
| Attribution | Fifteen copied-material records bind immutable reviewed upstream revisions, current hashes, modifications and exact MIT grants. Native source archives and original notices are retained. OpenSSL 3 replaces the problematic prebuilt BoringSSL path. | Import receipts do not establish unknown historical origins. Run material collection against every exact shipped image and obtain actual required source/relinking material. |
| Packaging | Complete source checkout or repository-built image is the supported service installation. Wheel help works; startup fails clearly when root policies/migrations/helpers are absent. | Standalone wheel installation is not supported. A future installer needs an explicit asset ownership contract. |
| Optional capabilities | Codex CLI pinned to 0.160.0; actual parser/version checks corrected resume sandbox argument placement. Public contract/support guides describe provisional and experimental interfaces. | Authenticated agents, mobile QA, cloud deployment, GPU/models and provider recovery remain unverified. |

SeaweedFS replaces the former MinIO server/source-image default at the maintainer's
request; the Apache-2.0 Python `minio` SDK remains the existing S3 client.
[Storage setup and migration](ops/seaweedfs/README.md),
[security follow-up](docs/open-source-security-followup.md),
[native build review](docs/openssl3-libdave-review.md) and
[support matrix](docs/support-matrix.md) describe the exact boundaries.

## Nice to have — can follow a cleared initial release

- Signed releases, SBOMs and reproducible image provenance.
- Dedicated provider test accounts, coverage targets, upgrade windows and operational SLOs.
- Versioned integration schemas after provisional contracts have been exercised.

## Decisions required from maintainers

- Select and enforce worker isolation and the capabilities included in the first release.
- Establish old credential use and coordinate remote/fork/cache/backup cleanup and rotation.
- Configure private reporting contacts and supported versions in the selected repository.
- Approve artifact-specific SDK/model/image rights and source/relinking bundles.
- Choose production storage operations, backup/restore, retention and TLS/IAM guarantees.
- Define provisional API/schema versioning, upgrades and supported platforms/providers.

Ownership confirmation is maintainer attestation. Employment/contractor agreements,
patents and each asset's original provenance were not independently inspected. It
cannot override third-party terms.

## Changes implemented

The project carries the official unmodified GNU AGPLv3 text and **AGPL-3.0-only**
first-party Python/npm declarations. Commercial use, modification, forks,
redistribution and self-hosting are permitted subject to the actual license,
including corresponding-source and applicable section 13 network requirements.
No CLA, license exception or custom resale/competitor/non-commercial restriction
was added.

Accumulated changes remove known sensitive information, private Maven defaults and
internal infrastructure assumptions; add generic configuration, secret generation,
public setup/contributor/security/conduct/release guidance and CI checks; harden
admission, webhook/attachment processing, sensitive logging, PostgreSQL roles and
QA delivery; document public contracts and experimental support; pin the CLI and
preserve imported material provenance. Deployed-state migrations are documented
in [migration guidance](docs/open-source-migration.md).

The latest changes install published SeaweedFS with private storage and explicit
verified migration, build pinned libdave 1.1.0/MLSPP/JSON source with OpenSSL 3,
retain original native notices/source receipts, and update README/configuration.
Real route checks exposed Discord's public configuration discarding the managed
command-secret reference. The API now validates that reference, preserves it on
partial updates and clears it explicitly on null. Migration 0137 normalizes
historical whitespace/empty values, validates the batch before writing and rejects
malformed data without logging values. Tests use actually created project IDs and
real webhook credentials instead of implicit-project assumptions.

## Licensing and attribution concerns

Third-party MIT/Apache/BSD/Boost/LGPL/MPL/CC grants and notices remain intact; root
AGPL does not replace them. SeaweedFS's source grant is Apache-2.0; complete
published-image components remain unreviewed. The active native build uses
OpenSSL 3, upstream libdave/JSON MIT, MLSPP BSD and bundled MPark.Variant Boost
notices. Do not redistribute historical BoringSSL voice binaries merely because
the current recipe changed. A deleted historical Apache frontend skill still
needs its original provenance/notices for any distribution containing that history.

See [notices](THIRD_PARTY_NOTICES.md), [inventory](docs/third-party-licenses.md),
[distribution material](docs/distribution-material.md) and
[attribution review](docs/open-source-attribution-followup.md).
Collectors report `collected-not-cleared`; a source-retrieval plan is not a completed
corresponding-source bundle. No blanket legal clearance is claimed.

## Verification evidence

Counts overlap; do not sum them into a full suite. Hosted CI was not run locally.

| Check | Result |
| --- | --- |
| Native/source/material/package contract regressions | **75 passed** in the final combined eight-file run. |
| Admission/security/configuration/Maven/source contracts | **110 passed**, two dependency deprecation warnings; separate actual-scanner canaries **3 passed**. |
| Discord public API and persisted JSON | **11 webhook tests passed** with three validation subtests; **9 migration tests passed**; Alembic checks passed. Tests include omission/null/replay, unauthorized use and no partial write on malformed later records. |
| Corrected admin/runtime fixtures | **132 passed**. No production default-project fallback added. |
| Broader deployment/storage/API run | **200 passed, 1 failed**, three subtests passed. The sole failure was a stale expected route hash after sensitive fixture text was replaced. Its two exact expected URLs were corrected; the unchanged production route code then passed the isolated regression **1/1**. The broad command was not repeated after this fixture correction. |
| Real route-smoke checks | **3 passed**, no skips. Synthetic stream handling in that harness remains unproven streaming coverage. |
| Real PostgreSQL final migration/RLS | Fresh runner **5 passed, 0 failed, 0 skipped** using current migration head and restricted roles. Disposable container/volume removed. This is not a live database upgrade. |
| Storage regressions and actual provider | **62 focused tests passed**. **11 real Docker checks passed**, including current guarded CLI copy/replay, actual GetObjectTagging support and rejection of real tagged source/matching destination objects. MinIO source and SeaweedFS destination retained exact synthetic bytes. Task-owned resources removed. Physical lifecycle expiry remains unverified. |
| Native arm64 compilation/linkage | Actual source build and CGo binary linked only `libcrypto.so.3`, OpenSSL **3.5.7**, Debian packages **3.5.7-1~deb13u3**, Go **1.25.14**. Eight installed-file and three source-archive hashes checked; all 19 retained Go source/lock files match. |
| Native crypto/startup | **18 native-tagged Go tests passed** offline with bounded memory/CPU, including real independent nonempty signed MLS key packages using production's transient-key contract. Network-disabled EOF startup emits `transport_ready` and exits 0. Final Docker gate/export, amd64/runtime/live audio remain unverified. |
| UI | **17 unit tests passed**, route type generation/TypeScript and production build **passed**. The final build supplied the documented loopback backend origin and an ephemeral randomly generated AUTH_SECRET without overwriting private configuration. Earlier attempts correctly rejected missing origin/weak local secret. |
| Python packaging | Fresh wheel/sdist build passed; 844 wheel/1,235 sdist entries and 759 Python sources byte-matched at inspection, migration0137 included, exact GNU license text and `License-Expression: AGPL-3.0-only`; no private env/runtime/node_modules paths. |
| Full Python suite | **262 passed, 1 failed**, two warnings; CPython AST-parser SystemError stopped the architecture test before its assertion. Exact isolated test then **1 passed**. The rest of the suite is unverified; no PR is being opened over this gate. |
| Global lint/format/quality | Ruff lint **passed**; **1,108 files already formatted**. Compatibility-shim, dead-code and TODO policy checks **passed**. First-party staged whitespace check passed; the full staged check flags original trailing whitespace in three byte-exact upstream license files, deliberately preserved rather than editing their text. **28 YAML / 3 TOML** documents parsed; **7 shell** files passed syntax; root-document local links resolved. |
| Fresh external setup earlier in this review | Frozen public installs, actual PostgreSQL/API source startup and **six real browser journeys** passed with zero retries. UI production build passed in that fresh snapshot. This evidence preceded the latest storage/native/0137 changes. |
| Optional audits | Earlier base Python/npm audits had zero findings. Optional voice retains three reports/two packages/two advisory IDs. Non-native Go had module x/crypto findings without reported reachable symbols; native reachability is unverified. |
| Core and final native container export | **Incomplete** because of local storage/snapshot-lease failures. Source dependency installation/compilation or independent tests do not prove completed final images or startup. No shared Docker resources were pruned. |
| Secret scanning | Pinned Gitleaks 8.30.1 scanned **1,605 candidate files**, zero findings, exit0. No symlinks, private package paths or files over5MiB found. Post-commit all-local-refs/full-history/reflog scan: **1,359 commits / 35.36MB, zero findings, exit0**. `git fsck --full` passed. The initial source push's remote SHA was verified equal to local HEAD and the repository remains private; old remote refs were not replaced. |
| Unreachable Git objects | 1,120 dangling blobs inspected: 1,118 without findings; two unmapped fixture blobs produced five matches. Whole-AST identity established their exact existing fixture paths; unchanged narrow rules rescanned the raw bytes at those paths with zero findings. No rule was broadened and all private snapshots were deleted. This is not a zero-finding unmapped scan. |

Heuristic scans and static review cannot establish absence of every secret or
confidential artifact. Only task-owned temporary snapshots, synthetic credentials,
proof containers/volumes and known disposable test resources were removed. User
volumes, private environment files, authentication state and unrelated Docker
resources were preserved. Resource failures are distinguished from code failures.

## Final pre-publication checklist

- [x] Remove identified private information/Maven assumptions and sanitize local history.
- [x] Apply official AGPLv3/AGPL-3.0-only and preserve reviewed third-party notices.
- [x] Document core source setup, public contracts, contribution and migration boundaries.
- [x] Implement private SeaweedFS storage and OpenSSL 3 native source builds.
- [x] Verify targeted security/configuration/migration tests and real PostgreSQL isolation.
- [ ] Resolve worker isolation and complete the full backend release suite.
- [ ] Clean old remote/fork/cache/backup history and rotate deployed credentials as required.
- [ ] Finish exact core/native image builds, supported-platform and real provider/audio checks.
- [ ] Clear each shipped artifact's rights, notices and corresponding-source/relinking bundle.
- [ ] Configure private reporting and rehearse production upgrades, restore, retention and ingress.
- [ ] Review the final archive/diff, exclude private runtime state and publish matching source.
