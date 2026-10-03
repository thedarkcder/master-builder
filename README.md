# Master Builder

Master Builder is a self-hosted, multi-tenant service that coordinates AI-assisted software delivery from Jira work items. It connects Jira and GitHub, queues work for execution workers, records workflow state and evidence, and provides a Next.js administration interface. Optional integrations support Discord, knowledge search, deployment previews and QA recordings.

This is an early project (`0.1.0`). Read the [readiness report](OPEN_SOURCE_READINESS_REPORT.md) before publishing or deploying it. API schemas and extension interfaces are provisional. Workers execute repository code and development tools: use isolated machines and grant credentials only for repositories you authorize.

## Requirements

- Python 3.11+ on a current patch release, [uv](https://docs.astral.sh/uv/getting-started/installation/), and Git.
- Node.js 20.9+ and npm for the administration UI.
- Docker Engine or Docker Desktop with Docker Compose v2 for local backing services. For first full runtime-image builds, budget at least 30 GiB of free host and Docker storage; dependency installation and layer export temporarily duplicate data. This is planning headroom, not a measured minimum.
- PostgreSQL with the `vector` extension; the supplied Compose stack provides it.
- A C compiler and `pkg-config` for native header contract tests in the Python
  suite. Debian/Ubuntu packages are `build-essential` and `pkg-config`; macOS
  developers need Xcode Command Line Tools and `pkg-config`.

You can run the local API and UI without Jira, GitHub, Discord or company infrastructure credentials. Executing delivery workflows requires your own Jira OAuth app, GitHub App installation, and an authenticated supported agent runtime. Mobile QA additionally requires platform-specific toolchains.

## Installation and support boundary

Use a complete source checkout or a runtime image built from this repository. The Python wheel is not a standalone service installer; runtime startup validates the required source assets and fails with setup guidance if they are absent. `uv build` checks packaging, not independent service installation.

See the [support matrix](docs/support-matrix.md) for platform prerequisites and validation limits. Mobile QA, voice and provider deployment packages are experimental; AWS/GCP directories are design specifications. The Docker runtime pins Codex CLI `0.160.0`; real authenticated delivery and provider recovery require separate verification.

## Quick start

Install the locked development dependencies:

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev python scripts/init_local_env.py
```

The initializer creates private `.env` and `admin-ui/.env.local` files with independent random passwords, signing secrets and a Fernet encryption key, plus `.runtime-home/seaweedfs/s3.json` containing environment references for storage credentials. It refuses to overwrite existing configuration. Inspect your own `.env` privately to find the initial administrator password, and see [configuration](docs/configuration.md) for optional settings. Compose reads `.env`; the Python service reads exported environment variables, and Next.js reads `admin-ui/.env.local`.

Start the core API and its backing services. This deliberately selects services instead of starting optional workers and public tunnels:

```bash
docker compose up --build -d api
curl --fail http://localhost:60001/health
```

The health response should contain `"status":"ok"`. Database migrations run through the Compose `migrate` service before the API starts. Container builds download public packages and browser tooling and can take several minutes.

Start the UI on your host in a second terminal:

```bash
cd admin-ui
npm ci
npm run dev
```

For a full-container UI instead, run `docker compose up --build -d admin-ui`. Compose supplies the internal backend origin `http://api:4000` and UI port `4100`; keep `AUTH_URL` set to the browser-facing URL.

Open [the local login page](http://localhost:60002/login) and sign in with `ORCHESTRATOR_ADMIN_USERNAME` and the password you generated. This core check does not run an agent or create external work items. The SMTP sink is available at [Mailpit](http://localhost:60005).

To stop the local services without deleting data:

```bash
docker compose down
```

For host Python development, export your local settings, point `ORCHESTRATOR_DATABASE_URL` at the host Postgres port, and use `ORCHESTRATOR_SMTP_HOST=127.0.0.1`, `ORCHESTRATOR_SMTP_PORT=60004`. Run migrations before the API:

```bash
ORCHESTRATOR_DATABASE_URL="$ORCHESTRATOR_MIGRATION_DATABASE_URL" uv run --frozen python -m orchestrator migrate
uv run --frozen uvicorn orchestrator.api.main:app --reload --host 127.0.0.1 --port 60001
```

Do not run the host API while the Compose API is using the same port. See [configuration](docs/configuration.md) for database credentials and host/container addresses.

## Using delivery workflows

1. Create a workspace (tenant) in the UI.
2. Connect your Jira OAuth application and select the projects you authorize.
3. Install your GitHub App into the intended repositories and configure repository allowlists.
4. Configure your agent runtime and authenticate it on an isolated execution worker.
5. Review project policy and ready statuses, then start the required worker processes.

See [GitHub and Jira onboarding](docs/github-app-oauth-onboarding.md), [public contracts](docs/public-contracts.md), [workflow decisions](docs/run-decision-engine.md), and [deployment packaging](docs/deployment-packaging.md). Secrets belong in the encrypted managed secret store or the documented runtime environment, never in repository files.

Discover commands without needing service credentials:

```bash
uv run --frozen python -m orchestrator --help
```

Typical commands after configuration:

```bash
uv run --frozen python -m orchestrator worker-runs
uv run --frozen python -m orchestrator worker-webhooks
uv run --frozen python -m orchestrator run --tenant TENANT_ID --issue PROJECT-123
uv run --frozen python -m orchestrator poll --tenant TENANT_ID
```

Worker startup is a separate operational step; local health does not prove that Jira delivery, agent execution or deployment previews are configured. Codex-backed execution needs the Codex CLI and its own authentication. A container worker can authenticate with `docker compose run --rm run-worker codex login --device-auth`. Keep that auth volume private. iOS execution requires a macOS worker and Xcode; it cannot run in Linux containers.

## QA recording storage and native voice

The local QA recording store uses the published **SeaweedFS 4.48** Docker image,
pinned by digest, through its S3 interface. Start it separately when needed:

```bash
docker compose up --build -d seaweedfs-init
```

Recordings remain private: the application uses scoped storage credentials, and
users download through authenticated routes that enforce tenant, project and run
ownership. The local lifecycle policy expires recordings after 30 days; physical
deletion and backup retention need operator verification. The Python dependency
named `minio` is an Apache-2.0 S3 client; it does not require a MinIO server.

Existing MinIO deployments need an explicit backup, object-copy verification and
configuration cutover before enabling the replacement. Keep the old volume until
the migration is verified; changing the endpoint alone does not move recordings.
See the storage setup and migration instructions in [ops/seaweedfs](ops/seaweedfs/README.md).

Native Discord voice builds **libdave 1.1.0** from pinned upstream source with
**OpenSSL 3**, rather than installing the upstream BoringSSL binary archive.
The build must retain dependency notices and source material; a successful compile
does not establish working Discord audio or clear every bundled component for
redistribution. Voice remains experimental. See [distribution guidance](docs/distribution-material.md)
and the [support matrix](docs/support-matrix.md) before building or distributing it.

## Development and checks

Track repository bugs, proposals and development work in [GitHub Issues](https://github.com/thedarkcder/master-builder/issues). See [CONTRIBUTING.md](CONTRIBUTING.md) for scope, verification and pull request guidance. Contributors do not need access to a private Jira project. Report vulnerabilities through [SECURITY.md](SECURITY.md).

```bash
uv run --frozen pytest
uv run --frozen ruff check orchestrator tests scripts
uv run --frozen ruff format --check orchestrator tests scripts
uv run --frozen python scripts/quality/check_compat_shims.py
uv run --frozen python scripts/quality/check_dead_code.py
uv run --frozen python scripts/quality/check_no_todo_markers.py
uv build
```

Frontend checks:

The public homepage documents Jira intake, worker/runtime configuration, workflow
decisions, QA evidence and GitHub review, with links to the source documentation,
component responsibilities and local setup. **Get Started** links to this
repository. Its GitHub star control reads the public repository count directly
from GitHub without credentials or cookies. It displays loading or **Stars
unavailable** when the repository is private, the response is invalid, or GitHub
cannot be reached; no substitute count is shown. The [privacy page](admin-ui/app/privacy/page.tsx)
describes this browser request. Homepage browser tests mock only the external
GitHub response and navigation destination.

Use the UI configuration generated in the quick start. Existing checkouts must add
`ORCHESTRATOR_API_BASE_URL=http://localhost:60001` to their private
`admin-ui/.env.local` before building; missing configuration stops the build.

```bash
cd admin-ui
npm ci
npm run test:unit
npm run lint
npm run build
npx playwright install --with-deps chromium
npm run test:e2e
```

Run `npm run build` before either browser suite: Playwright starts the production build with `next start` and fails if that build is absent. The default Playwright suite mocks backend responses; it validates frontend behavior, not end-to-end backend correctness. `npm run test:e2e:live` exercises a real disposable backend and database; read [CONTRIBUTING.md](CONTRIBUTING.md) before running it. Some baseline checks may fail; the readiness report records verification results and publication blockers.

## Architecture

- `orchestrator/api`: FastAPI routes, authentication and request schemas.
- `orchestrator/core`: policies, workflow decisions, runtime adapters and integration services.
- `orchestrator/storage`: SQLAlchemy models, PostgreSQL persistence and Alembic migrations.
- `orchestrator/tools`: governed Jira, GitHub, Git and repository bootstrap tools.
- `orchestrator/prompts`: packaged agent prompt templates.
- `admin-ui`: Next.js UI, Auth.js sessions and backend proxy routes.
- `discord_live_voice_transport`: optional Go voice transport.
- `deploy` and `ops`: deployment contracts and observability configuration.

PostgreSQL owns durable operational state. ClickHouse and OpenTelemetry provide observability. Optional Temporal orchestration coordinates workers; execution workers own repository toolchains. [Public contracts](docs/public-contracts.md) identifies supported entry points and provisional extension boundaries.

## Troubleshooting

- **Compose reports a missing variable:** fill the required entry in `.env`; do not remove the validation or paste a shared key.
- **API startup fails:** inspect `docker compose logs migrate api postgres clickhouse`. Check migrations, credentials, and the Postgres `vector` extension. Redact secrets and user data before sharing logs.
- **UI login fails:** check the API health response, matching admin credentials, `AUTH_SECRET`, and the UI backend address. Host UI uses `http://localhost:60001`; a container must use its internal API address.
- **Port is already in use:** stop the old process or adjust ports and update all API/UI callback and CORS URLs together.
- **A run cannot execute:** verify its project policy, repository allowlist, runtime authentication and worker platform. A successful health check is not runtime readiness evidence.
- **Browser tests cannot start:** install Chromium and its dependencies using the command above. Live tests require a running backend; mocked tests do not.

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Release notes are maintained in [CHANGELOG.md](CHANGELOG.md). Do not post credentials or exploit details in public issues.

## License

The project license is **GNU Affero General Public License version 3 only**, SPDX identifier **`AGPL-3.0-only`**. The official, unmodified license text in [LICENSE](LICENSE) governs use and distribution.

Commercial use is permitted: you may use, modify, fork, redistribute and self-host the software, including charging for products, hosting, support or services. The AGPL imposes corresponding-source requirements, including the requirements in section 13 for users interacting with certain modified versions over a network. This summary does not replace the license's terms and adds no restrictions.

Third-party dependencies and assets retain their own licenses and notices; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and the readiness report for attribution and unresolved rights questions. Contributions are normally made under the project's existing AGPL license; no Contributor License Agreement is required.
