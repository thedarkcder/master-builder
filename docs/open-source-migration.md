# Migration from internal development defaults

Back up the database and configuration privately and test this migration on a clone.
There is no compatibility mode for the removed defaults. `alembic.ini` no longer
contains shared database credentials. Use `python -m orchestrator migrate` with the
configured `ORCHESTRATOR_DATABASE_URL`; direct Alembic use requires an explicitly
configured `sqlalchemy.url`.

## Authentication and webhook keys

Set a unique admin password and independent random values of at least 32 characters
for `ORCHESTRATOR_ADMIN_TOKEN_SECRET`, `ORCHESTRATOR_AUTH_TOKEN_SECRET`, and UI
`AUTH_SECRET`. The `NEXTAUTH_SECRET` alias is removed. Restart API/UI/workers and
invalidate existing sessions; users must sign in again. Rotate all integration state
keys (`GITHUB_INSTALL_STATE_SECRET`, `ATLASSIAN_OAUTH_STATE_SECRET`,
`DISCORD_INSTALL_STATE_SECRET`, `JIRA_ACTION_TOKEN_SECRET`, each with the
`ORCHESTRATOR_` prefix). Pending OAuth/state links must be reissued.

`ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET` was not the settings field. Replace it with
`ORCHESTRATOR_ATLASSIAN_OAUTH_STATE_SECRET`. Set the canonical public API and UI
origins explicitly, using HTTPS for public deployments. Forwarding headers no longer
override the destination of security links.

For Jira, configure each tenant's `jira.webhook_secret_ref`. For Discord commands,
configure `discord.command_secret_ref`. Store corresponding values in the managed
secret store and configure provider requests to supply them. Unconfigured callbacks
are rejected before work is queued.

Migration `20261002_0137` normalizes persisted Discord command references before
the public schema validates them: whitespace is trimmed, blank strings become
explicitly unconfigured `null`, and malformed values block migration for operator
review. No reference is inferred from stored secrets. Valid references and other
Discord configuration remain intact. The migration is forward-only; use a reviewed
backup for rollback. The earlier settings API could discard this field, so inspect
existing tenants and explicitly reapply any missing reference using the authenticated
Discord settings PATCH endpoint. Omission preserves a reference; explicit `null`
clears it and disables command ingress. No live deployment was migrated here.

GitHub App webhooks use the configured platform signing secret (default reference
`GITHUB_WEBHOOK_SECRET`). Move any tenant-level webhook secret to the platform scope
and update the GitHub App's webhook configuration to match. Re-deliver pending events
after verifying signatures and routing; do not introduce an unsigned grace period.

## Existing encrypted secret records

If any deployed instance used the encryption key previously shipped in `.env.example`,
treat it as compromised. Do not overwrite that key first. In a restricted maintenance
window, decrypt records using the old key and re-encrypt them using a new generated
key. Verify every record, commit the migration atomically, then switch all processes
to the new key. Retain the old key only in a restricted recovery backup until recovery
is verified. This task does not access or migrate a live secret database; the
operator must implement and rehearse this deployment-specific migration before release.

## Local services and runtime data

`scripts/init_local_env.py` is only for a **new** checkout with no `.env` or UI local
environment file. It refuses to overwrite existing installations and never prints
keys. Export the host database URL for native Python; Compose uses the distinct
`ORCHESTRATOR_CONTAINER_DATABASE_URL` targeting `postgres:5432`.

Changing environment passwords does not change credentials already persisted in
Postgres, ClickHouse, Grafana or MinIO volumes. Rotate those accounts using each
service's administrative tools, then update consumers. Preserve data and confirm
access before restarting. Do not delete volumes to bypass migration.

Root Compose now publishes ports on `127.0.0.1`; Grafana anonymous access is disabled,
and Tailscale requires the explicit `tunnel` profile. Workflows default to
`workspace-write` sandboxing. Review worker permissions before permitting broader
execution. Hosted/public deployments require their own reviewed ingress configuration.

## Source/history

Local history was rewritten under explicit maintainer authorization. Branches, tags,
Codex snapshots and eight stashes were preserved as sanitized mappings; working-file
bytes and modes were checked before and after installation. Private generated paths,
static credentials and personal Git metadata were removed. Commit IDs changed;
invalidated commit/tag signatures must be re-signed if signatures are required.

The maintainer-authorized consolidation replaces `main` in the existing private
`thedarkcder/master-builder` repository with the sanitized source, using an exact
expected-old lease. All 29 inventoried old non-main heads are gone. A new Dependabot
Torch security update, opened from clean main during cleanup, remains separate.
All 386 old tags and 386 releases, including four assets, were removed under
explicit authorization. A verified private recovery bundle preserves sanitized
historical local work and all eight stashes. See the [consolidation review](branch-cleanup-review.md).

Other clones must migrate deliberately. Prefer a fresh clone after saving any
uncommitted work privately; do not merge or push old ancestry, tags or stashes into
the cleaned repository. Recover necessary branch-only changes individually and
review them for secrets and licensing before applying them to current main.

GitHub still serves the old main commit by its SHA and advertises pull-request refs.
Deleting branches and releases does not prove those references or cached objects
are erased. Review affected refs and pursue eligible sensitive-data removal through
[GitHub's documented cleanup process](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).
Fork/backup/CI-artifact cleanup and deployed credential rotation remain unresolved.
Keep the repository and other historical copies private until these publication
gates are cleared. Repository visibility has not changed.

Active ignored environment/authentication files and worker checkouts remain private
runtime state. Release from a reviewed commit or inspected distribution, with the
corresponding source and required third-party notices; do not archive the whole
working directory. Ownership attestation and SDK/model clearance are separate gates.
