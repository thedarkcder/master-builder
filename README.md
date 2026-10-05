# Open Factory

Open Factory is an open-source software factory: a framework for building your own
cloud-based engineering automation around the way your team works.

Define your engineering practices as workflows, policies, agent profiles and
review gates, then run them across your projects and repositories on infrastructure
you control. The project supplies the orchestration, administration and integration
tools to build on: work planning, configurable execution workers, project knowledge,
run inspection, GitHub review and optional deployment, QA and Discord integrations.

The factory is yours to design and extend. Choose the work it can accept, the tools
and repositories agents can access, the evidence each stage must produce and the
points where people must make a decision. Host the API, dashboard and workers in
your own cloud environment; local development uses the same source-based setup.
The public website contains project information, while the authenticated dashboard
belongs to each self-hosted installation.

This is an early project (`0.1.0`). Extension contracts are provisional, and cloud
provisioning and optional providers need their own validation. Read the
[readiness report](OPEN_SOURCE_READINESS_REPORT.md) before deploying it. Workers
execute repository code and development tools: isolate their environments and grant
credentials only for repositories you authorize. Installation/package identifiers
currently remain `master-builder`.

## Design your factory

| What you decide | Building blocks included |
| --- | --- |
| How your team turns work into a reviewed change | Planning, development, test and review workflows with recorded operations, attempts and human decisions |
| Which engineering practices agents must follow | Project policy, repository scope, required evidence and instruction resources |
| How work runs on your infrastructure | Worker processes, agent runtime profiles, governed tool access and platform-specific toolchains |
| What agents know about each project | Documents, knowledge assets, extracted facts and configured search/retrieval services |
| How people supervise the factory | Project/team access, run inspection, token telemetry, GitHub pull requests and configured Discord collaboration |
| What your factory does next | Source extension points for workflow handlers, stages, runtime adapters and QA recorder commands |

Start with the supplied interfaces and adapt them to your own engineering process.
Extensions currently require reviewing and changing source; there is no promise of
a stable plugin marketplace or a no-code factory designer. See
[what is included](docs/features.md), the [extension contracts](docs/public-contracts.md#extensions-and-protocols)
and [configuration](docs/configuration.md). Keep authorization, evidence and state
transition contracts intact when adding new automation.

Use the [local quick start](#quick-start) to learn the system before moving workers
and backing services into your cloud. The [support matrix](docs/support-matrix.md)
distinguishes supplied deployment tools from unvalidated integrations and design-only
cloud packages; the current release is not a managed cloud service.

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

Self-hosted dashboard checks:

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


## Public website

The public project website is a separate static application in [`public-site`](public-site/README.md),
deployed from `main` to [GitHub Pages](https://thedarkcder.github.io/master-builder/).
It has no login, session checks, backend connection or hosted dashboard. People run
the authenticated application on their own infrastructure. The project website
contains the factory overview, [feature catalogue](docs/features.md), source setup,
GitHub links and public-site privacy information.

```bash
cd public-site
npm ci
npm run lint
npm run build
npm run test:static
npx playwright install chromium
npm run test:e2e
npm run preview
```

Open `http://127.0.0.1:60003/master-builder/`. The generated `public-site/out/`
directory is the deployable artifact. The build requires no application credentials.
GitHub stars use an anonymous request to GitHub, with loading/unavailable states
and no invented counts. The public privacy page documents hosting logs and that request.

The public brand is **Open Factory**. Repository URLs, clone commands and runtime
identifiers currently retain `master-builder`; this website cutover does not rename
installed packages, database records or configuration keys.

## Architecture

- `orchestrator/api`: FastAPI routes, authentication and request schemas.
- `orchestrator/core`: policies, workflow decisions, runtime adapters and integration services.
- `orchestrator/storage`: SQLAlchemy models, PostgreSQL persistence and Alembic migrations.
- `orchestrator/tools`: governed Jira, GitHub, Git and repository bootstrap tools.
- `orchestrator/prompts`: packaged agent prompt templates.
- `admin-ui`: self-hosted Next.js UI, Auth.js sessions and backend proxy routes.
- `public-site`: standalone static project website deployed to GitHub Pages.
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
