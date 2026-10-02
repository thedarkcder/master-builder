# Security policy

Master Builder is pre-release software. There is no supported production release
or security response SLA yet; use the latest reviewed source and consult
[release blockers](OPEN_SOURCE_READINESS_REPORT.md).

## Reporting a vulnerability

Do not disclose credentials, tenant data, exploit payloads or security defects in
public issues. Use the repository's **Security → Report a vulnerability** channel
if maintainers have enabled GitHub private vulnerability reporting. Otherwise use
a private contact published by the maintainer account [thedarkcder](https://github.com/thedarkcder) on the repository profile. A working
private reporting channel must be configured before publication; this review cannot
invent a contact address or promise that reports will be received.

Include affected versions, reproduction steps, impact and a minimal sanitized
example. Share secrets only through an agreed confidential channel, and rotate
exposed credentials immediately. Maintainers should acknowledge, assess, fix and
coordinate disclosure without publishing reporters' personal information.

## Operating securely

- Use unique generated admin passwords, JWT/state signing keys, Auth.js session keys
  and a Fernet encryption key. Keep local env files, auth volumes and backups private.
- Use TLS and access-controlled ingress for deployment. Root Compose is for local
  use; its loopback ports are not an authorization boundary for processes on the host.
- Enforce request-body and authentication/reset/registration rate limits at ingress.
  Application-level limits and anti-abuse controls are not comprehensive.
- Configure signed Jira, GitHub and Discord callbacks. Missing webhook credentials
  are rejected; configure each provider before enabling its integration.
- Workers execute repository code, CLI tools and agent-selected actions. Give them
  isolated hosts, scoped credentials and restricted network/filesystem access. Do not
  accept arbitrary repositories or expose a worker's Docker socket to untrusted code.
- QA recordings, email inboxes, traces, model prompts and runtime logs may contain
  confidential data. Local SeaweedFS storage uses private recordings, scoped
  credentials and authenticated artifact delivery. Validate retention, backups and
  access controls for your actual production deployment.
- Review optional SDK/model licenses before distributing images or model artifacts.

## Existing deployments

Read [the migration guide](docs/open-source-migration.md) before applying these
changes. Rotating an encryption key without first re-encrypting existing secret
records makes them unreadable. Removing a file does not revoke a credential or
erase Git history, release artifacts, caches or forks.
