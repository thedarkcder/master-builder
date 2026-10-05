# Changelog

No public releases have been published by this preparation task.

- Use Open Factory as the public name and explain how teams design and extend
  cloud-based engineering automation using workflows, policy, agents and gates.
- Separate the static public website from the self-hosted dashboard; deploy only
  `public-site/out` to GitHub Pages and retain application authentication privately.

## Unreleased

- Make GitHub Issues the repository development backlog and remove private Jira
  access requirements from contributor and agent guidance. Historical issue import
  remains pending verified source-project access.

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
- Reject unsafe supplied navigation destinations before sign-in redirects,
  credential submission or archived-workspace continuation. Missing destinations
  retain the existing local routing policy.
- Reject malformed authentication sessions in JWT/session callbacks and the BFF;
  remove inferred administrator identity and require reauthentication.
- Describe Master Builder as an open-source software factory in the homepage,
  page metadata, README and feature guide.
- Replace the promotional homepage with a feature-first technical overview and
  source setup: planning, workflow execution, run inspection/token usage, project
  access, knowledge and delivery/collaboration integrations. Keep a compact delivery
  journey and label experimental features; add a source-backed feature guide. Remove
  the blog and apply a navy, blue and cool-gray design with responsive code blocks.

For each release, record user-visible changes, security fixes, migration requirements,
supported platforms and known limitations. Follow [release guidance](docs/releases.md).
