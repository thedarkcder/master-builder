# Release decisions requiring maintainer review

These gates apply to the affected capabilities. The maintainer explicitly authorized
local sensitive-history deletion and removal of private Maven configuration.
Publishing, remote force-pushes, live key rotation and deployment replacement remain
outside this task.
No Jira or Discord message was sent: this is a direct maintainer task without a
configured ticket or authorization to contact another person.

## Worker execution boundary

### Why a decision is required

The repository command validator does not isolate files, executable flags, ambient
credentials or subprocess resources. `workspace-write` configures an agent runtime;
it does not establish an operating-system boundary for every orchestrator tool.

### What we are trying to achieve

- Outcome: repository actions cannot read another tenant's files or host secrets.
- Who it impacts: self-hosters, workflow users and anyone offering hosted execution.
- Constraints: preserve intentional build capabilities, fail closed, enforce bounded
  execution and avoid a partial validator that suggests isolation it cannot provide.

### Options

**A — limited initial release:** support trusted repositories on dedicated workers
only. Disable or exclude unsupported hosted execution paths explicitly. Document
the threat model and test the admission boundary. This is smaller, but cannot offer
isolation between mutually untrusted tenants.

**B — isolated execution:** move every repository tool into a reviewed execution
environment with scoped mounts/credentials, filesystem and network policy, resource
bounds and explicit read-only versus write-capable actions. This supports a stronger
contract, but needs platform-specific integration tests and deployment migrations.

### Questions

1. Will the first release execute code from mutually untrusted users or repositories?
2. Which worker platforms and isolation mechanisms must be supported?
3. Which host paths, credentials and network destinations does a run actually need?
4. What subprocess time, output, memory and disk limits are required?

### Recommendation and verification

Choose A for a limited release until B is implemented and independently reviewed.
Documentation alone does not enforce A: gate unsupported paths before publishing.
Test malicious paths/flags, credential exposure, cross-tenant access, timeout and
output exhaustion against the real execution boundary. No implementation was
attempted for this unresolved security contract. Owners: maintainer and security.

## Private Maven deployment adapter — removal authorized

The maintainer selected removal of private Maven assumptions on 2026-10-02.
Generic Maven reactor builds remain supported. Projects must explicitly provide
Compose environment variables, profiles, resource credentials, dependencies and
health checks; the framework does not inject customer settings or alter packaged
Spring configuration.

Migration `20261002_0134` removes injected configuration only where captured
original planner evidence establishes its provenance. It preserves explicit
configuration and subsequent edits, and fails with a clear error when evidence
is missing or ambiguous. Operators must review and regenerate such plans before
retrying. No live deployment database has been migrated as part of this review.

The implementation and verification results are recorded in the readiness report.

## Local QA storage — SeaweedFS selected

The earlier pinned MinIO source builds resolved unavailable image tags but retained
an archived dependency. The maintainer subsequently selected SeaweedFS community
and its published Docker image. Preserve the S3 application contract, private
recordings, bucket-scoped credentials and retention. Existing deployments require
explicit copy, hash verification and cutover rather than a compatibility mode or
automatic deletion of the old volume. See [the implementation review](storage-native-release-review.md).

[MinIO upstream](https://github.com/minio/minio) is archived and declares that it is
no longer maintained. Production SeaweedFS operation still requires its own backup,
restore, retention, availability and security maintenance review.
