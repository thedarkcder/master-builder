# Configuration

The authoritative Python setting names, types and validation live in `orchestrator/core/config.py`. Each field maps to an uppercase environment variable prefixed with `ORCHESTRATOR_`, for example `admin_token_secret` maps to `ORCHESTRATOR_ADMIN_TOKEN_SECRET`. Unknown environment names are ignored by the settings library, so check spelling against that file.

Docker Compose reads the root `.env` automatically. Host Python processes require exported variables; merely copying the file does not configure them. Next.js reads `admin-ui/.env.local`. Treat both files as private and do not commit them. Compose ports and image settings use additional names such as `MASTER_BUILDER_API_PORT`, `POSTGRES_PASSWORD` and `SEAWEEDFS_ADMIN_SECRET_KEY`; inspect `docker-compose.yml` for their consumers.

## Required core settings

| Variable | Purpose |
| --- | --- |
| `ORCHESTRATOR_DATABASE_URL` | PostgreSQL SQLAlchemy URL (`postgresql+psycopg://USER:PASSWORD@HOST:PORT/DATABASE`); PostgreSQL needs the `vector` extension. |
| `ORCHESTRATOR_ADMIN_USERNAME` | Initial platform administrator username. |
| `ORCHESTRATOR_ADMIN_PASSWORD` | Independently generated administrator password. |
| `ORCHESTRATOR_ADMIN_TOKEN_SECRET` | Random signing key for platform administrator tokens, at least 32 characters. |
| `ORCHESTRATOR_AUTH_TOKEN_SECRET` | Separate random signing key for tenant user tokens, at least 32 characters. |
| `ORCHESTRATOR_SECRETS_ENCRYPTION_KEY` | Stable Fernet key used for encrypted managed secrets. Back it up securely with the database. |
| `ORCHESTRATOR_PUBLIC_API_BASE_URL` | Browser/provider-facing API URL for callbacks. |
| `ORCHESTRATOR_ADMIN_UI_BASE_URL` | Browser-facing UI URL for invitations and navigation. |
| `ORCHESTRATOR_CORS_ORIGINS` | Explicit comma-separated browser origins; authorize only your UI domains. |

For a fresh local checkout, the initializer creates both environment files with unique secrets and matching service credentials, plus `.runtime-home/seaweedfs/s3.json` with credential references:

```bash
uv run --frozen --extra dev python scripts/init_local_env.py
```

It refuses to overwrite an existing deployment. For manually managed environments, generate a new random value for **each** password/signing secret:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Generate the encryption key after installing dependencies:

```bash
uv run --frozen python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Enter generated values directly in your private environment files or secret manager. Do not post command output in issues. Changing a JWT signing key invalidates existing sessions; changing the Fernet key without migrating encrypted values makes those values unreadable. Existing deployments using a shared/default key need a planned secret rotation and re-encryption before publication.

## Host and container addresses

| Consumer | API | PostgreSQL | SMTP |
| --- | --- | --- | --- |
| Host UI or browser | `http://localhost:60001` | — | — |
| Host Python | — | `127.0.0.1:60003` | `127.0.0.1:60004` |
| Compose service | `http://api:4000` | `postgres:5432` | `mailpit:1025` |

The initializer creates independent migration-owner and runtime credentials. `POSTGRES_USER=orchestrator_migrator` and `POSTGRES_PASSWORD` initialize the owner; `POSTGRES_RUNTIME_PASSWORD` belongs to the non-owning `orchestrator_runtime` role. `ORCHESTRATOR_DATABASE_URL` and `ORCHESTRATOR_CONTAINER_DATABASE_URL` use the runtime role (host `127.0.0.1:60003`, container `postgres:5432`). `ORCHESTRATOR_MIGRATION_DATABASE_URL` and `ORCHESTRATOR_CONTAINER_MIGRATION_DATABASE_URL` use the owner and are only for explicit migrations. Never configure runtime consumers with the owner URL. Runtime connections reject superuser, BYPASSRLS, database/table ownership and membership in privileged/owner roles. Automatic startup migration defaults to false. `ORCHESTRATOR_CONTAINER_DATABASE_URL` is a Compose wiring variable, not a Python settings field. URL-encode password characters in connection URLs. A container's `localhost` refers to that container; do not use browser-facing localhost URLs for service-to-service requests.

The Python settings object does not read `.env` itself. To load a file you created and trust in a POSIX shell:

```bash
set -a
. ./.env
set +a
```

This executes the file as shell input, so keep exported values quoted and use it only with your own trusted configuration. Override container-only addresses before running host Python. Never source an environment file supplied by an untrusted repository.

## Administration UI

Copy `admin-ui/.env.example` to `admin-ui/.env.local` and set `AUTH_SECRET` to a new random signing secret. Configure `AUTH_URL=http://localhost:60002` for local host development. `AUTH_TRUST_HOST=true` is for a trusted local listener or correctly configured trusted reverse proxy; production proxies must prevent arbitrary Host headers.

`NEXT_PUBLIC_API_BASE_URL` identifies the backend used by the UI. It is public configuration, never a secret. Next.js may inline `NEXT_PUBLIC_` values when building; rebuild when changing them. `ORCHESTRATOR_API_BASE_URL` is the required runtime origin used by Auth.js, the BFF, and public form proxies. It must contain no credentials, path, query, or fragment. Host UI uses `http://localhost:60001`; Compose overrides it with `http://api:4000` and sets the internal UI port to `4100`. There is no fallback to the browser URL. Existing UI deployments must add this variable explicitly. See `admin-ui/lib/server-api.ts` for validation. The README quick start uses the UI on the host to keep its API address directly reachable.

## Optional integrations

| Integration | Required setup |
| --- | --- |
| Jira/Confluence | Your Atlassian OAuth app, managed client ID/secret, configured callback, authorized projects and `ORCHESTRATOR_ATLASSIAN_OAUTH_STATE_SECRET` for connection state. |
| GitHub | Your GitHub App ID/private key, installation and repository allowlist; `ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET` and webhook signing secret. |
| Discord | Your Discord app/bot, guild/permissions, interaction public key and installation state key when enabled. |
| Agent execution | Supported CLI/runtime installed on an isolated worker, authenticated runtime credentials and authorized repository tools. |
| Email | `ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER=smtp` plus SMTP host/port, or `resend` plus your own API key and verified sending domain. |
| Observability | ClickHouse endpoint/database/user/password and optional OpenTelemetry endpoint; Sentry DSN is optional. |
| Knowledge search | Public embedding model download/cache, configured Jira access for sync and sufficient local memory. |
| QA artifacts | Your S3 endpoint, access credentials, bucket and artifact URL configuration; review visibility before recording private data. |
| Deployment previews | Your Coolify instance/API token and explicitly configured deployment host, domains, callback tokens and project policy. |
| Voice | Optional Python `voice` extra, public model downloads, native Go/libdave transport and Discord configuration. |
| iOS/Android QA | Xcode/macOS for iOS; Android SDK, emulator/device and recording tools for Android. |
| Temporal | Reachable Temporal namespace/task queue and an orchestrator process when selecting that backend. |
| Tailscale | Your own auth key and deliberate tunnel configuration; do not start a public tunnel for the basic local quick start. |

To install the optional Python voice dependencies, use `uv sync --frozen --extra dev --extra voice`. Model downloads and the native Go/libdave transport are additional requirements; this command alone does not configure Discord or enable working audio.

Managed secrets are configured through authenticated administration interfaces and encrypted at rest. Reference names are configuration identifiers, not secret values. Consult [GitHub onboarding](github-app-oauth-onboarding.md), [deployment provider contracts](../deploy/common/env.required.md) and the actual settings/Compose files when enabling integrations. Never rely on access to the maintainers' services.

For the Discord command bridge, create a tenant-scoped managed signing secret and
set `discord.command_secret_ref` to the exact `secret_ref` returned by the secret
administration API. Configure the trusted bridge to send that secret in
`X-Webhook-Token`. The reference is exposed by the authenticated settings API:

```json
{"discord": {"command_secret_ref": "tenant/TENANT_ID/DISCORD_COMMAND_TOKEN"}}
```

Send this body to `PATCH /api/admin/tenants/TENANT_ID/discord` with an authorized
workspace session. Omitting the field preserves it; `null` explicitly clears it.
Blank or malformed references are rejected. This bridge token is separate from
Discord's interaction public key and installation state key; never put the token
value into `command_secret_ref`. See [migration guidance](open-source-migration.md)
for existing configurations.

Container profiles, optional workers and deployment packages are operational contracts that require explicit configuration. The AWS and GCP directories describe planned packages rather than a complete production-ready installer. Validate any provider package before advertising it as supported.

## QA artifact visibility

| Local storage setting | Purpose |
| --- | --- |
| `MASTER_BUILDER_S3_PORT` | Host loopback S3 port; default `60015`. |
| `SEAWEEDFS_ADMIN_ACCESS_KEY`, `SEAWEEDFS_ADMIN_SECRET_KEY` | Separate bootstrap/storage administrator identity; never worker credentials. |
| `SEAWEEDFS_ADMIN_PASSWORD` | Private backend administrator password, independently generated. |
| `SEAWEEDFS_S3_CONFIG_PATH` | Host JSON identity-policy file; generated `.runtime-home/seaweedfs/s3.json` contains environment references only. |
| `ORCHESTRATOR_QA_DEMO_ARTIFACT_ACCESS_KEY`, `ORCHESTRATOR_QA_DEMO_ARTIFACT_SECRET_KEY` | Scoped application Get/Put identity for the dedicated bucket. |
| `ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT` | Host S3 endpoint; locally `localhost:60015`, without scheme. |
| `ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT_INTERNAL` | Compose S3 endpoint; required `seaweedfs-s3:8333`. This wiring variable is not a Python setting. |
| `ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET` | Dedicated QA bucket; locally `qa-demos`. |

The local QA bucket is private. Its separate application account has Get/Put permission only for the dedicated bucket. `ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL` is the stable authenticated delivery prefix, locally `http://localhost:60002/api/bff/api/qa-artifacts`; it is not a raw bucket URL or anonymous signed URL. Reviewers must sign into the UI and have access to the artifact's tenant. API delivery also checks the exact project/run scope, serves single byte ranges, prevents HTML execution and disables shared caching. Worker proof checks authenticate to S3 directly.

`ORCHESTRATOR_QA_DEMO_ARTIFACT_RETENTION_DAYS=30` configures the dedicated bucket lifecycle (1–365 days); initialization replaces the lifecycle rules for that dedicated QA bucket. Current and noncurrent versions expire; provider scanner timing can delay physical deletion. Do not reuse a bucket with unrelated data or retention obligations. The API download cap defaults to 100 MiB (`ORCHESTRATOR_QA_DEMO_ARTIFACT_MAX_DOWNLOAD_BYTES`). Local storage uses a digest-pinned published SeaweedFS image with a private backend, separate authenticated S3 gateway and scoped Get/Put identity. See [local storage and explicit migration](../ops/seaweedfs/README.md) for setup, image provenance, existing-volume cutover and verification limits. Production TLS, provider maintenance, backups and real retention remain operator responsibilities.

Upgrade: stop QA producers, configure separate scoped S3 credentials and the authenticated delivery prefix, remove the old anonymous download policy, and apply migration `20261002_0136` with the owner credential. It rewrites persisted Run/checkpoint QA recording and failure-evidence URLs only when stored object keys match the actual tenant/project/run. Ambiguous evidence is blocked and requires recapture. Then regenerate existing external PR descriptions/comments, which the database migration cannot update. Previously published recordings, cached copies and backups require separate operator cleanup. Rerunning `seaweedfs-init` modifies the local dedicated bucket's access and lifecycle policies; review retention before doing so. Existing MinIO data is not attached or copied automatically: follow the explicit verified migration in the storage guide.

## Maven applications

The deployment planner builds a Maven reactor module and packages its single runtime JAR with Java 17. Declare the service environment, profiles, resource connection settings, dependencies, ports, and health checks explicitly in the deployment plan. It does not supply Spring profiles, authentication or payment settings, database roles, or application credentials. Resource names may be normalized to their Compose aliases; application configuration files remain unchanged. Zero or multiple eligible runtime JARs fail the build with a clear error. Supply a module that produces exactly one runtime JAR.

### Existing deployment plans

Alembic revision `20261002_0134` removes previously generated Maven configuration files and injected settings from persisted app, project, release, and analysis snapshots. Before upgrading an existing installation, make a private database backup and review its captured planner evidence. Run the normal `uv run --frozen alembic upgrade head` migration against your own configured database. This repository review tested isolated databases; it did not migrate any live installation.

The migration restores original explicit values only where the stored generated baseline proves they were overwritten. It preserves later user edits and generic resource aliases. Missing or conflicting evidence, modified generated build artifacts, or artifacts already present in the original user configuration stop the migration with an error that omits configuration values. Review and regenerate the affected plans with explicit application settings before retrying; do not discard ambiguous user configuration. Downgrading does not restore removed private defaults.

Database cleanup does not update previously committed deployment branches, exported Compose files, built images, running containers, or Git history. Review and regenerate those artifacts, rotate any exposed credentials, and deploy the corrected configuration separately before publication.


## Anonymous identity limits and trusted ingress

Public registration defaults to disabled. The local example explicitly sets `ORCHESTRATOR_PUBLIC_REGISTRATION_ENABLED=true` for onboarding; remove that opt-in on public deployments and invite users through an administrator. Shared database budgets count attempts independently of successful login/creation and survive transaction rollback. Limits apply across API workers. Account identifiers and peer addresses are stored only as keyed HMAC digests. Admission-store failure returns 503; exhausted budgets return 429 and `Retry-After`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `ORCHESTRATOR_AUTH_LOGIN_ACCOUNT_LIMIT` | 10 | Login attempts per normalized account per window, separately for admin/app login |
| `ORCHESTRATOR_AUTH_LOGIN_PEER_LIMIT` | 60 | Login attempts per ingress peer per window |
| `ORCHESTRATOR_AUTH_LOGIN_WINDOW_SECONDS` | 900 | Login/reset-confirm window |
| `ORCHESTRATOR_AUTH_RESET_ACCOUNT_LIMIT` | 3 | Reset email requests per account per window |
| `ORCHESTRATOR_AUTH_RESET_PEER_LIMIT` | 30 | Reset requests per peer per window |
| `ORCHESTRATOR_AUTH_RESET_WINDOW_SECONDS` | 3600 | Reset-request window |
| `ORCHESTRATOR_AUTH_REGISTRATION_PEER_LIMIT` | 5 | Registration attempts per peer/account per window |
| `ORCHESTRATOR_AUTH_REGISTRATION_WINDOW_SECONDS` | 3600 | Registration peer/account window |
| `ORCHESTRATOR_AUTH_REGISTRATION_GLOBAL_LIMIT` | 20 | Registration attempts per deployment per UTC day |
| `ORCHESTRATOR_TRUSTED_HOSTS` | localhost/loopback | Exact comma-separated allowed Host names; wildcards are rejected |

Identity JSON requests are limited to 16 KiB while streaming, passwords to 1024 characters, and reset/invite tokens to 4096 characters. These admission limits do not constitute CPU, billing, repository execution or tenant storage quotas. Tune measured load and abuse response with operators. Shared proxy peers can legitimately share the peer allowance; account limits still apply.

Set non-loopback API/UI/browser origins to HTTPS; plaintext origins are rejected except literal loopback development. Compose starts Uvicorn with `--no-proxy-headers`, so arbitrary forwarding headers cannot choose budget peers. Deploy a TLS gateway with exact Host validation and coarse connection/body/request limits. If forwarding peer addresses is required, explicitly configure Uvicorn to trust only that gateway's addresses, prevent direct backend access, and test spoofed forwarding headers. Never trust all proxies (`*`). The application cannot verify a gateway's TLS certificates, network ACLs or production termination from repository configuration.

## Existing database role migration

Existing PostgreSQL volumes do not rerun initialization scripts. Retain the existing bootstrap owner explicitly in `POSTGRES_USER`; do not rename a live owner by changing an environment variable. Create the separate runtime role with the owner-side `ops/postgres/20-runtime-role.sh` (it fails if that role already exists), using a new private `POSTGRES_RUNTIME_PASSWORD`. The script grants schema usage, table DML and sequence usage, with owner default privileges for future migrations. It does not grant role membership, ownership, schema creation or RLS bypass. If using a separately administered database, apply equivalent grants using your DBA's process.

Export your private owner migration URL explicitly and migrate before starting runtime consumers:

```bash
ORCHESTRATOR_DATABASE_URL="$ORCHESTRATOR_MIGRATION_DATABASE_URL" uv run --frozen python -m orchestrator migrate
```

Then restore `ORCHESTRATOR_DATABASE_URL` to the runtime URL, set startup migration false, and restart every API/worker consumer. Revoke old runtime use of privileged credentials. Changing role privileges on pooled connections requires a consumer restart. PostgreSQL catalog/role tests are authoritative RLS proof; SQLite tests do not exercise RLS. Application SQL context and database access are not a sandbox for untrusted worker commands; the separate worker-isolation release gate still applies.
