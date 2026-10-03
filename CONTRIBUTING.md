# Contributing

Master Builder welcomes issue reports, documentation improvements, tests and code contributions. Follow the [Code of Conduct](CODE_OF_CONDUCT.md). Submit security vulnerabilities through the process in [SECURITY.md](SECURITY.md).

## Set up a checkout

Follow the [README quick start](README.md#quick-start). Use Python 3.11+, uv, and Node.js 20.9+; install Python with `uv sync --frozen --extra dev` and UI packages with `npm ci` in `admin-ui`. Dependencies come from public registries. Do not commit `.env`, runtime authentication, local databases, worker checkouts, generated build output or logs.

The core API/UI can run without Jira or GitHub access. Workflows that contact those providers require applications and credentials you own. Do not use a maintainer's infrastructure to run tests.

## Scope a change

Use [GitHub Issues](https://github.com/thedarkcder/master-builder/issues) for repository bugs, proposals and outstanding development work. Search existing issues before opening one. Describe the problem, expected behavior, scope and how to reproduce or verify it; omit credentials, private infrastructure and personal/customer data. Keep changes focused. Discuss breaking contracts, migrations, architecture changes and significant security trade-offs before implementation. Link pull requests to the relevant GitHub issue. Private Jira access is not a contribution requirement. An open or imported issue does not authorize automatic execution; maintainers must approve scope, acceptance criteria and dependencies before implementation.

The application's Jira integration is a separate delivery capability. Moving this repository's development backlog to GitHub does not change a configured workspace's Jira intake.

Read [AGENTS.md](AGENTS.md), `.codex/POLICY.md`, `.codex/ENGINEERING_STANDARDS.md` and `.codex/OPERATING.md` for repository engineering expectations. Direct maintainer instructions take precedence over internal workflow defaults. Keep core decisions testable, side effects at integration boundaries and lifecycle transitions explicit. Do not introduce silent fallbacks, compatibility shims or placeholder production paths.

## Validate a change

From the repository root:

```bash
uv run --frozen pytest
uv run --frozen ruff check orchestrator tests scripts
uv run --frozen ruff format --check orchestrator tests scripts
uv run --frozen python scripts/quality/check_compat_shims.py
uv run --frozen python scripts/quality/check_dead_code.py
uv run --frozen python scripts/quality/check_no_todo_markers.py
uv build
```

`ruff format` without `--check` writes formatting changes; keep unrelated reformatting out of your contribution. For coverage use `uv run --frozen pytest --cov=orchestrator --cov-report=term-missing --cov-report=xml`. State baseline failures honestly; do not report skipped or partial verification as a passing full suite.

Generic Python tests explicitly use a migrated disposable SQLite database and a
private temporary runtime directory. Each test receives a fresh database copy;
test harnesses may select their own disposable resources. The suite overrides
inherited development database/runtime settings and does not load `.env` files.
The fixture removes its generated database/runtime resources after each test;
monkeypatch teardown runs before environment restoration and resource cleanup.
SQLite tests do not prove PostgreSQL row-level security: use the separate
`ORCHESTRATOR_RLS_TEST_DATABASE_URL` against a disposable PostgreSQL instance for
those checks. A temporary runtime destination does not prove that legacy runtime
migration cannot import credentials from the source home; that boundary remains
unverified. Do not run provider authentication or real agent execution tests using
a developer's existing authenticated home.

In `admin-ui`:

The quick-start initializer supplies the required UI environment. An existing
private `.env.local` must explicitly set `ORCHESTRATOR_API_BASE_URL` to the backend
origin before `npm run build`; use `http://localhost:60001` for the local host API.

```bash
npm run test:unit
npm run lint
npm run build
npx playwright install --with-deps chromium
npm run test:e2e
```

`lint` runs Next.js type generation and TypeScript checking. Both browser suites require `npm run build` first. Playwright starts the production build with `next start`; there is no development-server fallback. The default E2E suite provides deterministic frontend behavior coverage using backend mocks. It is not proof of database, authorization or full workflow correctness. For backend changes add tests at the owned backend boundary; mock only external systems.

The optional live suite connects to a real API and PostgreSQL, creates accounts/workspaces, sends test invitations and seeds records directly. Run it only against a disposable local test instance. Keep the virtual environment on `PATH` so its `python3` has psycopg available. Export the same runtime settings used by that instance, including admin credentials. Configure the UI's backend address to point at that API:

```bash
export PATH="$(pwd)/.venv/bin:$PATH"
export PLAYWRIGHT_API_BASE_URL=http://localhost:60001
cd admin-ui
npm run test:e2e:live
```

The live suite is opt-in and is not included in default hosted CI. `PLAYWRIGHT_API_BASE_URL` changes the test client's address; update `NEXT_PUBLIC_API_BASE_URL` (and the server backend setting if configured) in the UI environment as well. Never target production or a shared customer database.

The optional Go voice transport can be checked separately with `go test ./...` in `discord_live_voice_transport`. Building its native `libdave` mode requires the documented external toolchain in `orchestrator/Dockerfile`; Python installation does not install those native dependencies.

## Isolated PostgreSQL tenant isolation checks

Docker and the locked development dependencies are sufficient to run the real PostgreSQL RLS proofs:

```bash
uv run --frozen python scripts/verify_postgres_rls.py
```

The runner starts its own `pgvector/pgvector:0.8.6-pg16` container on a random loopback port, generates an ephemeral database-owner password, and creates restricted runtime roles through the integration tests. It discards inherited orchestrator configuration, so it cannot select your development or customer database. Readiness probes are bounded and the runner removes only its own container and anonymous data volume. It fails on test failures, missing results or any skipped test. Hosted CI runs the same command. PostgreSQL extensions/roles and real transaction/pool isolation are exercised; this does not prove provider deployment or worker execution.

## Pull requests

Include the user-visible problem, implementation summary, exact verification commands/results and risks. Include regression tests for fixed bugs, validation/failure cases, and real-backend UI evidence where appropriate. Identify database migrations and explain operational effects. Do not merge or publish automatically.

When changing dependencies, update `uv.lock` or `admin-ui/package-lock.json` using the relevant package manager, inspect the dependency and license changes, and rerun the affected checks. Never replace a failed frozen install with an unlocked install.

Contributions are accepted under **AGPL-3.0-only**, the project's existing license, unless an explicit file notice states otherwise. Preserve third-party notices. Submit only material you have the right to contribute. No CLA is currently required.

## Releases

See [CHANGELOG.md](CHANGELOG.md) and [OPEN_SOURCE_READINESS_REPORT.md](OPEN_SOURCE_READINESS_REPORT.md). Maintainers should verify a fresh checkout, resolve licensing/attribution and history blockers, update both package versions, record compatibility and migration notes, and tag the reviewed commit. Publishing a tag or source archive does not automatically publish a Python/npm package or deployment.
