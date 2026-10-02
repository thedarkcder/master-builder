# Local private QA storage

The local stack uses the published SeaweedFS 4.48 image, pinned to multi-platform index digest `sha256:4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d`. Upstream release source is commit `530be3e37337488ecc34d58441e0bc476e121c93`: [official release](https://github.com/seaweedfs/seaweedfs/releases/tag/4.48). SeaweedFS is Apache-2.0; that license and its component notices remain separate from this project's AGPL-3.0-only license. The S3 SDK's `minio` package name does not require a MinIO server.

The server runs as UID/GID 1000 on a new `seaweedfs-data` volume. Its master, filer and administrator endpoints are reachable only on the internal storage network. A separate S3 gateway joins the application network and publishes only a loopback development port. The JSON identity file contains environment references, never credentials. Its mode 0644 permits container UID 1000 to read a host-owned file; the containing generated directory and credential files remain private. Do not expose the filer, master, administrator or S3 port publicly.

## Fresh setup

From a complete source checkout:

```bash
uv sync --frozen --extra dev
uv run --frozen python scripts/init_local_env.py

docker compose up -d seaweedfs seaweedfs-s3
docker compose run --rm seaweedfs-init
```

The initializer refuses to overwrite `.env`, `admin-ui/.env.local` or `.runtime-home/seaweedfs/s3.json`. The credentials belong in the private `.env`; Compose requires them explicitly. Application credentials permit only bucket listing/location and object Get/Put in the dedicated QA bucket. They cannot delete objects, change policies, create buckets or access another bucket. Bootstrap uses a separate administrator identity, removes any anonymous bucket policy and persists current/noncurrent expiration at 30 days (configurable 1–365). Rerunning bootstrap preserves objects while replacing this dedicated bucket's policies/lifecycle. Never share it with unrelated data or legal-hold requirements.

The unsigned gateway returns 403, including for its health probe. Successful authenticated bootstrap establishes operational readiness. Artifact links remain authenticated API/UI URLs; do not substitute anonymous S3 links.

## Explicit upgrade from existing MinIO storage

Do not run the fresh initializer over existing configuration. Stop QA producers and prevent all other writes to the source and destination until verification and cutover finish. Retain a private source backup, the existing volume identity and the exact old image/endpoint needed to restore it. The new Compose stack does not attach, delete or modify the old MinIO volume. Do not delete that volume until you have independently verified the migration and retention decision.

1. Add `MASTER_BUILDER_S3_PORT`, `SEAWEEDFS_ADMIN_ACCESS_KEY`, independently generated `SEAWEEDFS_ADMIN_SECRET_KEY` and `SEAWEEDFS_ADMIN_PASSWORD`, and `SEAWEEDFS_S3_CONFIG_PATH` to the existing private `.env` using `.env.example` as the setting reference. Generate new scoped application credentials. Never copy old root credentials into the application account.
2. Export the bucket name and create a new identity file exclusively. This file contains environment references, not their values:

   ```bash
   export ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET=qa-demos
   uv run --frozen python - <<'PY'
   import json, os
   from pathlib import Path
   from orchestrator.core.qa.storage_setup import identity_config
   path = Path('.runtime-home/seaweedfs/s3.json')
   path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
   with path.open('x') as stream:
       json.dump(identity_config(bucket=os.environ['ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET']), stream)
   path.chmod(0o644)
   PY
   ```

3. Start and initialize the new dedicated storage with the fresh-setup Compose commands above. Keep the existing source service or an operator-managed isolated service mounting the old volume available with its original credentials. No obsolete server is supplied as a default or automatic substitute.
4. Supply source and destination administrator credentials through environment variables only: `QA_MIGRATION_SOURCE_ACCESS_KEY`, `QA_MIGRATION_SOURCE_SECRET_KEY`, `QA_MIGRATION_DESTINATION_ACCESS_KEY`, `QA_MIGRATION_DESTINATION_SECRET_KEY`. Avoid recording shell commands containing values. Run:

   ```bash
   uv run --frozen python scripts/migrate_qa_storage.py \
     --source-endpoint "$SOURCE_S3_ENDPOINT" \
     --destination-endpoint "$DESTINATION_S3_ENDPOINT" \
     --bucket "$ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET" \
     --confirm-source-quiesced
   ```

   Endpoints are `host:port`, without schemes or credentials. TLS is required by default; `--source-insecure` and `--destination-insecure` are explicit exceptions for isolated loopback development services. The default per-object cap is 100 MiB; set `--max-object-bytes` deliberately if required. Source credentials need List/Get/stat/versioning and `GetObjectTagging` access; destination credentials need List/Get/Put/stat/versioning and `GetObjectTagging` access. Unsupported or unauthorized tag inspection fails clearly. The migration refuses tagged source or matching destination objects, versioned/suspended buckets and changed/different destination objects; it never silently drops historical versions or overwrites unrelated data. It validates artifact object-key structure, bounded stream lengths, SHA-256 bytes, content type, custom object metadata and source size/ETag again. A failed run authorizes no cutover. Replay rechecks previously copied objects; it never deletes source objects.
5. Verify the aggregate copy/replay counts and read migrated artifacts with the scoped application identity. Change `ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT_INTERNAL` to `seaweedfs-s3:8333` and the host endpoint to the configured loopback S3 port. Preserve the bucket/object keys and authenticated delivery prefix. Restart consumers only after verification. Existing private delivery URLs do not require another database migration; apply the existing `20261002_0136` URL migration if upgrading from old anonymous evidence, and update external PR links separately.

Tagged source/destination objects and versioned buckets are explicitly rejected. Tags, historical versions, legal holds, object locking, replicas and backup retention need an independently designed preservation migration; this tool is for this application's ordinary unversioned, untagged recording objects. All writers must remain stopped: listing is not an atomic snapshot of keys added concurrently.

## Verification boundaries

[Security follow-up](../../docs/open-source-security-followup.md) records 62 passing focused tests. The original nine-check Docker proof predates the tag guard; a subsequent nine-check repeat uses the current guarded host CLI and verifies real source/destination tag-query support. The final lightweight eleven-check repeat also proves that real tagged source objects and tagged matching destination objects are rejected without a successful equivalence claim; it used existing images and the current host CLI. Thirty-day lifecycle configuration is verified; physical deletion after thirty days, production TLS/IAM exposure, provider operations, backups and long-term security maintenance require separate operator verification. The lifecycle worker's scheduling/scanner cadence can delay deletion. Published image provenance is pinned, but this is not a blanket review of every container component or a production safety declaration.

## Staff review

### 0. Reality model

Fact: QA object keys and authenticated artifact URLs remain canonical. Event: the maintainer selected SeaweedFS to replace archived local MinIO source builds. Artifact: policy JSON references private environment credentials. Observation: a disposable gateway reaches 403 before authenticated initialization. Storage owns bytes; the application owns tenant/run authorization. State: configured → private initialized → copied/verified → explicit cutover. Root cause: obsolete local server selection, not an API proxy failure. Anti-patching verdict: change the owned storage boundary and migration only.

### 0.5 Design search

Compared an all-in-one server on the application network, a private backend plus S3 gateway, and an operator-only external endpoint. The all-in-one would expose unauthenticated filer/master APIs to workers; external-only would remove the requested usable local setup. Chosen private backend/gateway adds one small container while preserving S3 contracts. Rejected legacy `Write` permissions because they include deletion; explicit attached policies preserve minimal Get/Put. Greenfield and deletion tests retain only storage ownership, bootstrap and explicit migration. Exhaustion gate satisfied for local support; actual policy denial or missing retention support would reopen the decision.

### 1. Problem

Provide usable local private storage without making archived MinIO the default or losing existing recordings.

### 2. Type

Security and portability contract migration.

### 3. Invariants

- Anonymous and out-of-bucket access fail.
- Application identities cannot administer or delete.
- Migration never overwrites differing data or deletes source objects.
- Missing primary configuration fails; no server/credential fallback exists.

### 4. Assumptions

Local experimental operation uses loopback/private networks. The dedicated bucket contains ordinary unversioned, untagged QA recordings. Tagged or versioned/held data requires a separate preservation plan; unsupported tag inspection fails without a compatibility path.

### 5. Contract matrix

Fresh configuration → private bootstrap; missing keys/config → startup failure; matching copied object → verified replay; different/changing/truncated/oversized object → failure before cutover; tagged source/destination or versioned bucket → explicit rejection; unsupported tag query → clear failure. Before: archived source server. After: immutable published SeaweedFS, stable S3/API keys.

### 6. Call-path impact scan

Compose/hybrid startup invokes bootstrap; QA workers and API use the unchanged SDK endpoint/settings. Operators invoke migration explicitly; no startup auto-copy.

### 7. Domain term contracts

Public artifact base means authenticated delivery, not public bucket. Administrator identity means initialization/migration ownership, never worker identity. Retention means provider lifecycle policy, not proven instantaneous physical deletion.

### 8. Authorization & data-access contract

Static administrator credentials and attached least-privilege application policy are separate. The master/filer network is isolated; application traffic reaches the authenticated S3 gateway. API/UI authorization remains independently enforced.

### 9. Lifecycle & state matrix

Existing volume remains untouched. New volume receives private bootstrap. Quiesced copy is hash-verified and replayable. Only the operator changes endpoints. Failures preserve source and prohibit cutover.

### 10. Proposed design

Pure identity builder and bounded migration logic in `storage_setup.py`; CLI side effects in scripts; pinned init adapter and Compose edges in `ops/seaweedfs`.

### 11. Patterns used

Composition, explicit configuration, scoped policies, bounded streams and integrity verification. Rejected legacy action grants and automatic compatibility mode.

### 12. Patterns not used

No anonymous signing fallback, generalized storage abstraction rewrite, live data mutation or deletion-based synchronization.

### 13. Change surface

Local Compose, environment initializer/examples, hybrid fingerprints, new bootstrap/migration scripts and focused storage tests/docs. Artifact API/key schema remains unchanged.

### 14. Load shape & query plan

Migration enumerates objects and uses sequential bounded 64-KiB reads; several stat/hash requests per object trade speed for integrity. No relational queries or persistent new schema.

### 15. Failure modes

Missing configuration, denied operations, retention mismatch and stream/source mutation fail clearly. Keep writers stopped, preserve source and repair the explicit contract before retrying.

### 16. Operational integrity

Existing volume rollback remains operator-owned; no automatic attachment/deletion. Image pins and dependency hashes prevent accidental upgrades. Configuration generation refuses overwrites. Concurrent writers invalidate migration assumptions and must be stopped.

### 17. Tests

Owned identity/bootstrap/migration regressions plus realized Compose/hybrid/init tests; isolated actual container proof checks credential scope, replay, retention and legacy-provider copy. UI behavior is unchanged and covered separately by authenticated artifact regressions; no mocked UI is used as storage evidence.

### 18. Verdict

✅ Proceed — design is appropriate and scoped
