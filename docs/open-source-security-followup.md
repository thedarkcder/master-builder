# Security follow-up design and evidence

## Local navigation destination validation

The UI previously accepted a slash followed by a backslash as a local `next`
destination. An actual Auth.js-signed request reproduced a redirect to an external
origin. The archived-workspace continuation link also accepted protocol-relative
destinations. The proxy, login page, authentication provider and archived-workspace
page now share one pure URL
validator: supplied destinations must resolve to a canonical root-relative local
path. Backslashes, controls, malformed escapes and authority forms are rejected
with a fixed diagnostic. Invalid supplied values never select an alternative
route; absent values retain the existing default routing policy.

The provider validates before changing authentication state or submitting
credentials. The login page displays an error and disables submission; signed-in
login/register requests return HTTP 400 without a redirect. Invalid archived-page
destinations display an error without a continuation link. Valid local queries
and fragments remain supported. No destinations are persisted, so this correction
requires no data migration. Credential validation and tenant authorization remain
separate contracts.

The original real-session regression and archived-link regression failed before
their fixes. The final combined browser gate passed 117 tests, including 21 local
navigation tests without mocking session verification or the proxy. All 45 UI
unit tests passed, including rejection before provider side effects.
These checks establish navigation validation, not live backend login or complete
worker isolation. The readiness report records the final combined UI checks.

## 0. Reality model
The reviewed baseline exposed anonymous account creation, password-based login and reset endpoints without shared admission budgets. Four API workers make a process-local limiter insufficient. MinIO initialization granted anonymous downloads. Runtime PostgreSQL used the same credential as the migration owner, which can bypass RLS. The actual fresh PostgreSQL catalog check identified missing forced RLS on `workflow_executable_work_items`. These are configuration and authorization defects, not project-lifecycle defects. The API owns identity admission, storage owns database privilege validation, and QA storage owns object retrieval. No live environment or deployed data is changed by this work.

## 0.5 Design search
Considered process-local counters, an external Redis service, PostgreSQL admission counters, and edge-only limits. Process-local counters multiply allowance across workers; Redis introduces another mandatory system; edge-only limits cannot enforce account/global quotas independently of proxy topology. PostgreSQL atomic conditional upserts reuse the primary dependency and survive authentication rollbacks. A greenfield system could use a dedicated gateway, but deleting that model here leaves the same missing application quota. Chosen trade-off: a small database write on anonymous authentication. Evidence of unacceptable measured database load would justify a separate admission service. For artifacts, public URLs and expiring anonymous signed URLs were rejected: stable authenticated API delivery preserves reviewer access only with an authorized account. This requires reviewer onboarding. No anonymous fallback is permitted.

## 1. Problem
Public identity and artifact ingress need bounded, authenticated contracts, and database RLS needs an enforceable runtime-role boundary.

## 2. Type
Security and operational contract correction.

## 3. Invariants
- Authentication failures consume a durable budget across workers.
- Public registration is disabled unless explicitly enabled and has a global creation budget.
- Runtime connections cannot use superuser, BYPASSRLS, database owner or protected-table owner credentials.
- Artifact retrieval requires authenticated access to its exact tenant/project/run; raw bucket access is private.
- Existing dirty work, live credentials and live databases remain untouched.

## 4. Assumptions
SQLite is only an explicitly enabled disposable test contract. PostgreSQL is the production admission store. HTTP is for loopback development; external ingress must use TLS and exact trusted hosts. MinIO is a dedicated QA bucket. Retention is an operator-controlled policy with an explicit bounded default, not perpetual evidence storage.

## 5. Contract matrix
| Input | Before | Required behavior |
|---|---|---|
| Repeated incorrect login | Unbounded | Shared account and peer limits; 429 with Retry-After |
| Registration without opt-in | Creates tenant | 403 before account allocation |
| Admission database unavailable | No budget dependency | Fail closed; no memory substitute |
| Privileged runtime database role | RLS silently bypassed | Clear connection rejection |
| Anonymous QA URL | Download allowed | Authentication required |
| Authorized artifact outside requested run | No delivery API | 404 without storage disclosure |

## 6. Call-path impact scan
Admin/app login, reset request/confirmation and registration call one admission adapter. Runtime session factories validate each physical PostgreSQL connection; migration engines use separately supplied owner credentials. QA recording URLs are stable delivery URLs; worker evidence checks authenticate directly to configured object storage. Compose initializes a private dedicated bucket and retention policy.

## 7. Domain term contracts
Admission means permission to attempt an operation, not successful login or tenant creation. A quota counts attempts, so transaction failure does not refund it. Runtime role means a non-owning data-access role, not the migration administrator. QA artifact means an object under the authenticated run's tenant/project/run prefix.

## 8. Authorization and data-access contract
Budget identifiers are HMAC digests rather than stored emails/IP addresses. Identity-auth context alone may update admission rows. Tenant users cannot read budgets. Artifact ACL checks precede storage access. RLS is defense in depth against application scoping errors; it does not confine arbitrary SQL issued with ambient application privileges. Worker isolation remains an existing separate release gate.

## 9. Lifecycle and state matrix
Budget absent → admitted/count 1; active below allowance → increment/admit; exhausted → reject until the fixed window expires; expired → new window. Role validation runs before using a pooled physical connection. Bucket bootstrap explicitly removes anonymous access; retention expires objects, making old proof unavailable rather than silently publishing elsewhere.

## 10. Proposed design
- Focused admission module, atomic database upserts and migration 0135.
- Thin endpoint calls and validated positive settings.
- Focused runtime role audit at the database boundary.
- Separate migration/runtime credentials in local initialization and Compose.
- Authenticated scoped artifact delivery, private MinIO policy and documented retention/migration.

## 11. Patterns used
Explicit primary configuration, immutable budget inputs, conditional upsert, transaction-local RLS context and streaming object delivery.

## 12. Patterns not used
In-memory fallback, permissive proxy header trust, anonymous signed-link fallback, compatibility credentials, project-lifecycle rewrites or live environment mutation.

## 13. Change surface
Settings, authentication controllers, admission storage/migration, runtime database boundary, local environment generator/Compose, QA storage/delivery and scoped regression tests. Existing contributor changes are retained.

## 14. Load shape and query plan
One indexed primary-key upsert per budget dimension, plus bounded indexed expired-row cleanup. No account-table scan is needed for admission. Artifact downloads stream bounded chunks instead of materializing videos in memory.

## 15. Failure modes
Exhaustion returns 429; disabled registration returns 403; database failure fails closed. Privileged roles require provisioning a separate runtime role and restarting consumers. Expired/missing artifacts return 404 and require new QA evidence. Existing anonymous bucket policies must be explicitly removed on upgrade.

## 16. Operational integrity
Migrate using an owner before starting consumers with non-owning runtime credentials. There is no downgrade that disables security policies. Admission updates commit independently of request transactions. Existing proof URLs need operator migration/regeneration when changing the canonical delivery origin. No live data or storage lifecycle is altered automatically by repository edits.

## 17. Tests
Regression tests must exercise actual shared SQL counters, authentication routes, failure budgets, explicit registration gating, PostgreSQL role/catalog contracts and authorized artifact scope. Root owns isolated real PostgreSQL/browser proof. Results and remaining operator gates will be appended after execution.

## 18. Verdict
✅ Proceed — design is appropriate and scoped.


## Implemented contracts and current evidence

Migration 0135 creates shared admission counters and enables/forces the executable-work-item policy found missing by the real PostgreSQL catalog test. Migration 0136 migrates only known QA fields in Run execution snapshots and workflow checkpoints, using joined persisted run scope; ambiguous proof is explicitly blocked for recapture. Required configuration and existing-database/S3 upgrade steps are in [configuration.md](configuration.md).

Focused core checks initially passed 55 tests; additional actual persisted JSON migration, global registration quota and host-rejection tests passed 13. Red evidence included registration returning 201 without opt-in, third invalid login returning 401 instead of 429, missing role enforcement, no streamed auth-body bound, anonymous artifact delivery and use of public HTTP for worker probes. Root's isolated PostgreSQL catalog/context suite passed 6 tests on non-superuser runtime credentials through revision 0135. Its final runtime/provisioning runner passed 5 tests with zero failures/skips, including a role owning a public table while an empty schema appears first in search_path; the canonical public-schema audit rejected that role.

The broader QA/auth/migration attempt stopped at 5 failures with 131 passes. Two missing-setting failures and two trusted-Host fixture mismatches were introduced by the stricter primary contract and have been corrected. The remaining default-project lifecycle failure is outside this security change and is not evidence of a successful all-repository suite.

The local artifact bootstrap's previous Docker image pins could not be pulled from Docker Hub or the official Quay registry. The maintainer explicitly selected pinned upstream source builds for local development. Upstream [MinIO source-only distribution](https://github.com/minio/minio#source-only-distribution), [security-fix release](https://github.com/minio/minio/releases/tag/RELEASE.2025-10-15T17-29-55Z) and [lifecycle configuration](https://github.com/minio/minio/blob/master/docs/bucket/lifecycle/README.md) informed the source/retention contract. Archived upstream availability and maintenance do not constitute production support. The isolated real bucket proof below verifies the local policy contract; production provider access and physical deletion still require operator proof. Mocked S3 transport tests prove owned API authorization and streaming.

## Remaining operator and maintainer gates (updated local provider below)

- Review the selected provider's production TLS/IAM exposure, retention, versioning, legal holds, replicas/backups and scanner timing. Prior MinIO source-image evidence below is historical; the current local provider and migration contract are documented in the SeaweedFS section.
- Confirm TLS termination, exact trusted ingress/proxy addresses and direct-backend firewalling in the real deployment. No public certificate/gateway proof was performed.
- Set intentional registration opt-in and admission allowances; establish execution/billing/tenant storage quotas, abuse alerting and recovery policy. Those product policies are not invented here.
- Migrate owner/runtime credentials on existing volumes, revoke privileged runtime use and restart all consumers. No live database or credential was changed by this repository work.
- Update existing external PR proof links after URL migration; revoke historic anonymous policies and handle already copied recordings separately.
- Worker ambient owner/application credentials and filesystem/network capabilities remain an independent unresolved isolation gate. RLS is not a boundary against malicious code executing under the application role.


## Final scoped verification (before source-container proof)

- Focused admission/role/host/body/artifact/configuration suite: 60 passed, including strict migration-schema replay rejection and realized generated Compose forwarding.
- QA service + migrations/startup + admission and QA migration regression suite: 199 passed (2 existing dependency deprecation warnings). This includes repeated migrations after deliberately rewound stamps; existing admission schema must exactly match columns/types/PK/check/index before replay, and RLS is reapplied. Unexpected schema fails explicitly.
- Generated environment plus realized Compose configuration: 5 passed; the subprocess excludes inherited variables defined by the generated template, so contributor test settings cannot override the exact deployment configuration under test.
- Repository Python Ruff check passed; hybrid Bash and role-provisioning shell syntax checks passed.
- Existing tenant-user suite retains a separate default-project workflow failure; the all-repository suite is not declared green.
- Live PostgreSQL/browser and local source-container evidence are owned by the root/portability reviewers and must be appended with their exact final results.

The anonymous admission adapter resides at the API boundary (`orchestrator/api/auth_admission.py`), avoiding a new core-to-web dependency. Its atomic counters commit in their own scoped identity-auth transaction; expired-row cleanup is indexed and limited to 100 rows per admission. No process-local substitute exists. Default Uvicorn proxy behavior is also hardened in the experimental Hetzner profile; that profile still requires operator-provided non-owning runtime credentials and TLS/Host configuration before use.

Post-review boundary regressions first failed in six cases, then passed all 142 admission/private-artifact/runtime-role/QA-service checks. Range endpoints are bounded before integer conversion; QA storage timeouts must be finite and positive; invalid pool size/overflow/timeout/recycle settings fail validation instead of being silently clamped. Explicit settings-shaped test adapters declare the required timeout. Repository Ruff lint passed and all 1100 Python files passed format validation after these changes.

Hybrid startup builds both local source-storage images and fingerprints their recipes. A missing build fingerprint now requires a build; existing image names alone cannot establish source provenance. Image availability resolves each exact Compose image name, including explicit local source tags, instead of guessing project-derived tags.

The hybrid/storage configuration regressions passed 23 tests after three new red cases. Existing static expectations for removed development database credentials and anonymous QA URLs now assert explicit required configuration. No legacy default-project behavior was modified. Shell syntax validation and repository Ruff lint/format checks passed after this final script change.

Runtime ownership auditing targets the canonical `public` schema explicitly. A caller-controlled `search_path` cannot switch the catalog audit to an empty schema and omit ownership of application tables. The DBAPI boundary regression first failed on `current_schema()` and then verifies rejection and transaction rollback with the public-schema audit.

The final canonical-schema role change passed 12 runtime/database tests after its negative regression failed as expected. Whole-repository Python lint and format checks remained green (1100 files). These boundary checks do not replace live non-superuser PostgreSQL isolation proofs or deployed TLS/storage inspection.

An isolated real MinIO/MC container proof passed eight checks using the verified pinned upstream source commits: exact filtered Compose image tags; loopback rootless health; actual repository bucket/user/policy/lifecycle initialization; separate application identity Put/stat/Get with exact synthetic bytes; anonymous Get denied 403; unrelated-bucket retrieval and administration denied; current/noncurrent expiration exported as 30 days; and initialization replay preserving private access and existing data. The reviewer inspected the harness and aggregate evidence. Its disposable Compose project, containers and data volume were removed. Both final immutable-source recipes subsequently built successfully for linux/arm64, and the same eight runtime checks passed again against both final images. Metadata checks verified Go 1.27.1, UID 10001, notices/source archives/module manifests and current Dockerfile hashes. Thirty-day physical expiration, versioning/legal-hold/backups, production service maintenance and real deployment access remain operator gates.

## Scanner effectiveness gate

`scripts/verify_secret_scanner.py` requires an explicitly supplied checksum-verified Gitleaks 8.30.1 CLI and an available OpenSSL CLI. It neither installs tools nor substitutes another scanner. In a mode-0700 disposable directory it generates a real ephemeral RSA key, appends it after the actual GitHub parser fixtures, and generates a valid random Fernet key for the canonical encryption setting. The gate requires both dedicated rule IDs at their exact canary paths and Gitleaks finding exit status 1. It invokes `--ignore-gitleaks-allow` and complete redaction, prints only aggregate evidence, and deletes generated keys/reports on success and failure.

Security CI verifies the pinned archive checksum, runs this gate, then runs the existing full-history scanner. Local verification uses `python3 scripts/verify_secret_scanner.py --gitleaks /absolute/path/to/verified/gitleaks`; to run its actual-tool regression tests, supply `MASTER_BUILDER_GITLEAKS_BINARY=/absolute/path/to/verified/gitleaks uv run --frozen --extra dev pytest tests/test_secret_scanner_canary.py`. Without that explicit integration-test binary the actual-tool tests are visibly skipped; the required CLI gate itself fails if tools are unavailable, incompatible or miss either key. No scanner effectiveness claim follows from a skipped test.

The new gate tests failed first (three cases), then all three passed using the verified real CLI: missing tool fails clearly, fresh keys are detected despite existing fixture text, and configuration omitting the dedicated rules is rejected. This verifies these canaries, not universal secret detection or absence of confidential data.


## SeaweedFS local storage replacement

The maintainer explicitly selected SeaweedFS rather than keeping archived MinIO source builds as the local default. Current Compose uses the published multi-platform SeaweedFS 4.48 image pinned to its immutable index digest, a private backend network and a separately authenticated S3 gateway. The former MinIO/MC source Dockerfiles are removed; the earlier eight MinIO checks above describe the superseded implementation only. The Python `minio` package remains the existing Apache-2.0 S3 SDK, not a server dependency. See [current setup, explicit migration and staff review](../ops/seaweedfs/README.md).

Identity JSON contains environment references only; independently generated administrator and application credentials are required. The application attached policy permits only dedicated-bucket List/location and Get/Put, with no legacy broad `Write` action. The bootstrap revokes anonymous bucket policies, persists current/noncurrent retention and treats only `NoSuchBucketPolicy` as an already-private state; other provider errors remain fatal. Existing environments and old volumes are never overwritten, attached or migrated automatically. The explicit migration rejects tagged source or matching destination objects, versioned buckets, different destination data and changed/truncated/oversized streams, preserves content type and custom metadata, verifies SHA-256, and retains all source data. Writers must remain quiesced; tags/locks/versions/backups require a separately designed preservation plan.

Fresh focused storage/private-artifact/environment/deployment/hybrid tests passed 57 cases (two existing dependency deprecation warnings). The bootstrap import regression, absent-policy provider state and migration metadata-preservation regression each failed before their fixes. Repository Ruff lint passed; all 1,108 Python files passed format validation, and hybrid Bash syntax passed.

The final isolated Docker proof passed nine boundaries using the exact published SeaweedFS image and rebuilt initializer: rootless private-backend/loopback-gateway health; actual Compose bucket/lifecycle initialization; application Put/stat/Get with exact synthetic bytes; anonymous Get/Put denied 403; Delete/admin/other-bucket/unknown-key/wrong-secret denied; current/noncurrent expiration persisted at 30 days; initialization replay preserving data/privacy; random administrator/application credentials absent from captured container logs; and the documented CLI copying and replay-verifying a disposable legacy MinIO source with SHA-256 integrity and source objects retained. The proof used a host-owned mode-0644 environment-reference file read by container UID 1000. Its project, containers, networks, temporary policy file and new volumes were removed; no existing user volume or environment was touched. Earlier provider failures were investigated without broadening the IAM policy. A legacy-source health/readiness race was fixed in the disposable harness with bounded authenticated readiness, not with a product fallback.

These checks verify local policy and migration behavior; they do not demonstrate thirty-day physical deletion, a comprehensive container-component audit, or real production TLS/IAM/provider operations. Those remain explicit operator/distribution gates.


### Migration tag-preservation guard

Five new regressions failed before the guard/CLI fix, then the focused storage/artifact/deployment suite passed 62 tests. Migration now requires authorized `GetObjectTagging` on both providers, checks source tags before copy and again after verification, and checks matching destination tags before claiming replay equivalence. Tagged objects require a separately designed preservation migration. Unsupported or unauthorized tag inspection fails with a safe owned diagnostic naming `GetObjectTagging`; raw provider messages remain withheld. No silent tag omission or compatibility path exists.

The original nine-check Docker proof predates this guard. A lightweight repeat using already-built images and the current host migration CLI passed all nine checks with real source/destination tag queries; no image was rebuilt or exported. This establishes that the guard does not block ordinary untagged migration on the selected providers. The final lightweight repeat passed eleven checks: the previous nine checks with current guarded host CLI plus actual source-object tags rejected before replay and actual matching destination tags rejected before any equivalence claim. Both providers support the required tag queries. The already-built images were reused in a fresh disposable project without rebuilding or exporting images; all newly owned containers/networks/volumes were removed afterward. No user storage or environment was modified.
