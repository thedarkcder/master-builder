# Public contracts and stability

Master Builder is at version `0.1.0`. The entry points below are intentional, but route schemas, workflow formats and extension interfaces are **provisional**. Do not assume semantic versioning guarantees for internal Python imports or database tables. Maintainers should document changes and migrations in release notes before external consumers depend on them.

## HTTP interfaces

The running FastAPI service exposes `/docs` and `/openapi.json`, generated from actual route schemas. Use them to inspect current request/response shapes. Most HTTP interfaces exist under `orchestrator/api/routes` and use Pydantic models from `orchestrator/api/schemas.py` or `deployment_schemas.py`.

| Interface | Intended consumer and authorization |
| --- | --- |
| `GET /health` | Local monitoring; checks basic API availability, not complete worker/integration readiness. |
| `/api/admin/*` | Platform administration and authorized workspace administration. Platform admin login is `POST /api/admin/auth/login`; protected routes accept authenticated tokens and, where implemented, platform HTTP Basic auth. |
| `/api/app/auth/*` | Tenant-user login and principal/session identity. Tenant access is scoped by memberships and permissions. |
| `/api/public/*` | Public account registration, invitation and password-reset flows; each route has its own validation/token contract. |
| `GET /runs/{run_id}` | Protected run lookup; knowing a run ID grants no access. |
| `POST /jira/webhook/{tenant_id}` | Jira ingress for a configured tenant, protected by that tenant's configured webhook authentication. |
| `POST /github/webhook` | GitHub App webhook ingress; configure and validate webhook signatures. |
| `POST /discord/interactions` | Discord interaction ingress; configure the app public key/signature validation. |
| `/api/internal/deployment-hosts/*` | Deployment host protocol with host-specific credentials; internal operational contract. |

Authorization is route-specific. Inspect OpenAPI and `orchestrator/core/security.py`; do not infer that every administration route has identical role or credential requirements. Keep admin credentials out of frontend bundles. Browser sessions are handled by Auth.js and server-side backend proxy routes in `admin-ui/app/api`; those proxy routes are implementation details, not a supported replacement API.

## Command line

`python -m orchestrator --help` and the installed `orchestrator` entry point expose the parser in `orchestrator/cli.py`. Intended operator commands include migrations, manual run enqueueing, queue inspection and worker processes. `agent-tool` is a governed runtime bridge requiring resolved tenant/project/run/stage context, not a general unrestricted shell interface.

CLI output, exit codes and argument schemas are provisional. Missing configuration should cause an actionable failure rather than a guessed tenant, repository or auth identity. Background workers require independent supervision and explicitly authorized execution environments.

## Configuration and persistence

Environment variables in [configuration](configuration.md) and the supplied example files are operator-facing configuration. Secret references must point to values in the encrypted managed store or explicitly configured resolution paths. Project/tenant JSON configuration and Pydantic request schemas should be versioned before downstream users rely on them.

PostgreSQL tables, ORM models and Alembic revision identifiers are implementation details. Apply supported migrations; do not hand-edit schema state. The live test helper that seeds a project directly into PostgreSQL is test scaffolding and does not define a public persistence API.

## Extensions and protocols

- Agent runtime/provider adapters coordinate model execution and governed tool use; native CLI authentication/tool support must be available on the selected worker platform.
- Stage plugins (`orchestrator/core/stages`) and the workflow handler/type registries are provisional internal extension points. A plugin must obey project policy, explicit state/transition ownership and the required artifact contracts.
- Deployment contracts live in `docs/deployment-packaging.md`, `deploy/common` and provider packages. Coolify callback/host-agent protocols are operational interfaces, not currently a stable external SDK.
- The Go voice transport exchanges process-boundary messages with the voice runtime. Native dependency/toolchain requirements are separate from the Python package.
- QA recorder commands are explicit extension hooks with worker-platform, artifact and verification contracts; installing a command does not establish proof that a recording or deployment succeeded.

## Maven deployment

Maven deployments use the selected reactor module to build exactly one runtime JAR.
The generated runtime image executes that packaged JAR without copying, editing or
replacing application configuration. Projects supply their own Compose environment,
Spring profiles, database/resource credentials, service dependencies and health checks.
A public website is not required merely to build a Maven service. Ambiguous runtime
JAR output fails rather than selecting an arbitrary artifact.

## Boundaries to stabilize before adoption

Maintainers should decide a supported API versioning/deprecation policy, publish stable integration schemas, define supported runtime/toolchain versions and document migration guarantees. Multi-tenant isolation, secret scope and repository allowlists must remain enforced across each adapter. Until those decisions are made, integration authors should pin a reviewed release and rerun their contract tests on upgrades.
