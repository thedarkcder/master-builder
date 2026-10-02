# Changelog

No public releases have been published by this preparation task.

## Unreleased

- Declare first-party software as `AGPL-3.0-only`; add license and third-party review.
- Add external setup, contribution, configuration, security and public-contract docs.
- Require configured authentication keys and canonical origins for security links.
- Reject callbacks without configured authentication and remove GitHub tenant-secret
  fallback in favor of the platform GitHub App signing secret.
- Harden local service bindings and credentials, Docker contexts and contributor CI.
- Replace internal fixture examples and remove local review artifacts.
- Apply compatible dependency advisory fixes; unresolved advisories remain release
  blockers recorded in the readiness report.
- Replace the local MinIO server with private SeaweedFS S3 storage, with explicit
  object migration and retention requirements for existing deployments.
- Build pinned libdave source with OpenSSL 3 instead of installing BoringSSL archives;
  retain native dependency/source evidence and experimental voice status.
- Expose and preserve the Discord command signing-secret reference in authenticated
  configuration; migration 0137 normalizes existing references without guessing them.

For each release, record user-visible changes, security fixes, migration requirements,
supported platforms and known limitations. Follow [release guidance](docs/releases.md).
