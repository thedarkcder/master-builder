# Open-source portability follow-up

This scoped review addresses reproducible runtime tooling, installation expectations and optional-platform claims. It does not certify provider deployments, model redistribution or standalone wheel operation.

## 0. Reality model

Source checkout and runtime Docker targets include root configuration and `.codex` assets. The Python wheel packages `orchestrator` and prompt templates, but omits root configuration, runtime policy assets and helper scripts. Migration and agent execution depend on those files. The initial image selected a moving Codex npm tag; the follow-up pins a reviewed stable release. Provider plans describe intended functionality rather than verified release support.

## 0.5. Design search

Considered packaging every operational asset into the wheel, silently searching alternative locations, and validating the existing source layout. Packaging all assets expands distribution and configuration ownership; implicit searches violate the fail-hard contract. Choose a single explicit source layout and actionable validation. A verified standalone installation design would justify revisiting this choice.

## 1. Problem

External contributors need reproducible tools and truthful installation and platform expectations.

## 2. Type

Integration boundary.

## 3. Invariants

- Required operational assets are validated before runtime side effects.
- CLI help requires no service authentication or provider contact.
- Runtime tooling uses exact reviewed release versions.
- Optional capabilities do not imply validated production support.

## 4. Assumptions

The initial release supports source checkout and the repository's Docker source layout. No standalone wheel promise is inferred from a successful wheel build. Validation uses local assets, unauthenticated offline CLI parsing, and a separately owned disposable PostgreSQL container.

## 5. Contract matrix

| Input | Expected behavior |
| --- | --- |
| Complete source layout | Runtime may proceed to its normal configured prerequisites. |
| Missing root assets / standalone wheel | Clear failure directing the operator to a full checkout or built repository image. |
| CLI help | Usage displayed without database, credentials or network. |
| Optional platform tooling absent | Capability remains experimental/unavailable until prerequisites are installed and validated. |
| Moving Codex npm tag | Replaced with an exact public stable release, checked during build. |

## 6. Call-path impact

CLI dispatch, API startup, migration setup and Codex runtime asset synchronization depend on the operational source layout. Runtime Docker targets retain the same layout. No tenant/project policy, schemas or database records change.

## 7. Domain contracts

“Buildable wheel” means a Python distribution artifact; it does not mean an independent complete service installer. “Prerequisite check” proves local availability, not provider authorization or a real workflow. “Experimental” identifies unverified integration/runtime paths.

## 8. Authorization and data access

Direct maintainer-authorized portability work. No external messages, provider authentication, cloud deployment or existing-database mutation. PostgreSQL proofs use only an owned disposable container. Existing dirty work is preserved.

## 9. Lifecycle

Validate source assets -> validate configured runtime prerequisites -> start runtime. Missing assets terminate before runtime work; no fallback layout or compatibility shim is introduced.

## 10. Proposed design

Add one source-layout validator, wire it at runtime boundaries, correct the Codex repository root, pin the image CLI and document the support matrix.

## 11. Patterns used

Pure filesystem validation, explicit runtime boundary checks, test-first negative regressions and bounded local subprocess smoke checks.

## 12. Patterns rejected

Wheel architecture rewrite, alternate asset lookup, credential defaults and unauthenticated provider smoke deployments.

## 13. Change surface

Focused source-layout module and runtime call sites; Codex Docker argument/build smoke; portability/support documentation and regression tests. No data migration is needed because this changes no persisted representation or settings: incomplete installations fail explicitly.

## 14. Load shape

A fixed small set of local asset checks at startup; no queries or network fan-out.

## 15. Failure modes

Incomplete checkout, incorrectly assembled images, missing CLI binary/version drift, absent native tools and unverified provider provisioning. Errors direct users to the supported installation without printing secrets.

## 16. Operational integrity

Source/layout validation is reversible code. No state rewrite or automatic rollout. Operators retain their explicit configuration and authenticate integrations separately. Third-party binary/model redistribution remains governed by its own licenses and notices.

## 17. Verification plan

Negative source-layout and runtime-root regressions first; valid source layout and CLI help; narrow affected CLI/startup/runtime suites; exact Codex version/help smoke in a temporary install; local optional-tool inventory. Record actual outcomes and limitations below.

## 18. Pre-implementation verdict

✅ Proceed — design is appropriate and scoped.


## Verification outcomes

- Required source assets are checked before CLI dispatch, API lifespan, migration database access and Codex runtime state preparation. The corrected runtime root resolves the source checkout rather than the `orchestrator` subdirectory. All three image targets copy the same required bootstrap/QA helper files.
- An isolated built wheel displayed help successfully (exit0) and rejected runtime execution with actionable full-checkout guidance (exit2), without a traceback or importing the command runtime. No standalone service install is claimed.
- Exact public Codex release `0.160.0` installed in an owned temporary directory and reported `codex-cli 0.160.0`. Exec help exposes required flags. Actual new-session and resumed-session command shapes were parsed without `--help` and stopped at an explicit invalid provider before authentication/model execution. The old `--full-auto` resume flag failed parsing and was removed; sandbox selection now uses the supported exec-parent option. Explicit read-only, workspace-write and danger-full-access command shapes were checked; no compatibility path was added.
- Docker build argument validation accepts an exact stable release and rejects `latest`, prereleases and version ranges. All20 Compose build arguments use the same default, and environment examples declare the exact version. Existing moving-tag overrides require an explicit operator configuration update.
- Source/CLI/runtime/Docker/startup affected suite:71 passed before the additional CLI-resume correction. Codex/source/runtime/Docker subset after that correction:69 passed. The recorded OpenTelemetry entry-point deprecation warning is third-party; no test was skipped.
- Wheel build, scoped Ruff lint/new-file formatting, diff checks, compatibility/dead-code/no-marker policies passed. A complete rebuilt image after these changes remains unverified; Docker build verification is gated by available storage and native/image attribution review.
- Local prerequisite inventory on macOS arm64: Docker29.6.2, Xcode27.0/simctl, adb1.0.41, Java17.0.9, Maven3.9.12, FFmpeg8.0.1 and Go1.27.1 returned successful checks. libdave was absent. No actual mobile capture, Discord encrypted audio, cloud provisioning or inference was tested by that inventory.
- The repeatable isolated PostgreSQL runner exercised the three real tests in `tests/test_tenant_rls_postgres.py`:3 passed,0 failures,0 skips; owned container/data-volume cleanup succeeded. The same runner is added to hosted CI; the hosted job itself has not run here. It checks test result XML and never prints captured tracebacks containing ephemeral URLs.
- Owned temporary Codex/wheel smoke directories were removed after attribution capture. Aggregate non-secret evidence was retained outside the repository. Shared caches and services were untouched.

## Post-implementation verdict

✅ Proceed — design is appropriate and scoped.

## Additional local object-store image design

Official MinIO/MC binary image pulls are unavailable in the reviewed environment. The authorized primary contract is to build from immutable official upstream source archives, verify their SHA256 digests and preserve upstream licensing and source material. There is no mirror or older-image fallback. GitHub's official comparison confirms the selected MinIO head descends from the release fixing CVE-2025-62506. Both upstream repositories are archived, so this enables experimental local development; it does not establish a maintained production storage option.

The image boundary uses pinned multiarchitecture Go and Debian base digests, CGO disabled, source-defined dependencies without upgrades, rootless runtime processes, and the existing server/client commands. Required curl and shell utilities remain available for Compose health/init operations. Preserve upstream archives, vendored module sources and notices, module/build manifests and the build recipe; distribution still requires reviewing full corresponding-source and third-party obligations. Actual build and object-store tests will be recorded separately, with resource/toolchain failures reported rather than substituted binaries.

## Independent session-lifecycle and test-state review

PostgreSQL claims are transaction-local (`set_config(..., true)`). The owning operation's Session reapplies its explicit claims through `after_begin`, so commits and rollbacks preserve the operation identity while returned pool connections lose it. Inspection found that upstream SQLAlchemy `Session.close()` retains `Session.info`; therefore same-instance reuse could restore a preceding operation's identity. A real disposable PostgreSQL regression reproduced this failure (2 passed,1 failed,0 skipped), before the root agent introduced the project-owned `RLSSession` lifecycle. Its close/reset/invalidate clear only the RLS claim key in `finally`, preserving unrelated session metadata. Both owned storage and workflow factories select that class. Direct upstream `Session` reuse is not a supported authenticated operation boundary.

The PostgreSQL fixture creates a unique database and independent LOGIN role with NOSUPERUSER/NOBYPASSRLS/NOINHERIT/NOCREATEDB/NOCREATEROLE, grants object access separately, and asserts the actual runtime role attributes. The lifecycle test uses the product factory, pool capacity one, tenant switching, commit/rollback, same-object close/reset/invalidate reuse, then a fresh Session. A second workflow-factory test covers its actual ownership boundary. Disposal precedes dropping only the unique fixture database/role. The repeatable runner rejects remote Docker endpoints before creating resources, explicitly selects the inspected local Unix socket for every Docker operation, requires loopback binding and a nonempty zero-skip test report, and removes its exact created container/data volume.

Tests isolate implicit legacy-runtime reads through module-local filesystem inputs, leaving the process-wide HOME and the human-authored production migration intact. Explicit synthetic repository/HOME snapshots remain usable, and the existing end-to-end migration test proves fresh auth/config restoration, volatile-state exclusion, asset synchronization and preservation of later runtime-owned files. The new isolation boundary test failed before the fixture change; the14 runtime-home/source tests then passed. No developer credential contents were read for this proof.

No existing operational database or external identity provider was modified by this review. Real browser authentication proof belongs to the separate root-agent verification; this session/storage review does not claim broader frontend or deployment coverage.

Final scoped proof: the product-factory PostgreSQL suite passed4tests,0failures,0skips in its owned disposable container, including session close/reset/invalidate and workflow factory reuse. Source/tool/runtime tests passed71tests after default legacy-source isolation. Whole Python Ruff formatting checked1100files and formatted932; initial grouping of Delete targets in two unchanged test files was restored to their original raw AST with statement-only formatting exclusions. Final AST differences0, full Python Ruff lint/format checks passed. Aggregate formatter evidence remains outside the repository; this pass preserved existing functional dirty edits.

## Final source-image and object-storage proof

Both final immutable-source recipes built successfully within the600-second per-image bound on Linux arm64. MinIO reports commit `7aac2a2c5b7c882e68c1ce017d8256be2feea27f`; MC reports `77f82e18b5401a65958f1619df6ebb994634bd88`; both report Go1.27.1 and the correct upstream copyright year. Container checks verified UID10001, unmodified upstream/Go notice files, valid upstream/vendor source archives, module graphs/build manifests, and image-contained Dockerfile hashes matching the current recipes. AMD64 and other architectures were not built here.

The fresh final-image disposable Compose proof passed8checks: exact service-filtered image resolution; rootless loopback health; the actual Compose private-bucket/scoped-user/lifecycle initialization; separate application identity Put/stat/Get with exact payload bytes; anonymous Get denied403; unrelated bucket/admin access denied; exported current/noncurrent30-day expiration; initialization replay preserving private access and existing application data. Only generated process-local credentials and synthetic objects were used. Owned project containers, network and data volume were removed. This validates local storage bootstrap and access boundaries, not production operation or complete dependency/license clearance.

Verification harness corrections remained outside product behavior: health readiness tolerates a connection closing during startup within its bounded deadline, and Docker stdin attachment was corrected so `mc pipe` receives the intended synthetic payload. The exact byte assertion was retained. A final client rebuild was cancelled when host storage fell below2GiB; after removing only identified owned compile-cache records, both final builds and the fresh final-image proof completed. Shared caches, existing services and existing storage volumes were not modified. Superseded initial image-ID removal found no images to remove; final source tags remain available for further verification/attribution. Aggregate non-secret evidence is retained outside the repository.

This historical MinIO proof is superseded by the maintainer-selected published SeaweedFS local provider. Its old source-build recipes are removed. [Current storage setup and explicit verified migration](../ops/seaweedfs/README.md) preserves the old source volume and requires operator-controlled cutover. No existing user data volume was migrated during this review; prior MinIO checks do not establish the current provider contract.
