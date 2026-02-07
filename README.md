# master-builder

Multi-tenant Jira-driven agent orchestrator service.

## Requirements
- Python 3.11+
- Jira MCP access configured in your Codex environment

## Local setup
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

Set required environment values:
```bash
export ORCHESTRATOR_ADMIN_USERNAME=admin
export ORCHESTRATOR_ADMIN_PASSWORD=change-me
export ORCHESTRATOR_DATABASE_URL=sqlite:///./orchestrator.db
export ORCHESTRATOR_CORS_ORIGINS=http://localhost:4100,http://127.0.0.1:4100
```

## Public API
- `GET /health`
- `POST /jira/webhook/{tenant_id}`
- `GET /runs/{run_id}` (admin-auth protected run lookup policy)

## Admin API
- `GET /api/admin/tenants`
- `POST /api/admin/tenants`
- `GET /api/admin/tenants/{tenant_id}`
- `PUT /api/admin/tenants/{tenant_id}`
- `DELETE /api/admin/tenants/{tenant_id}`
- `POST /api/admin/tenants/{tenant_id}/test-jira`
- `POST /api/admin/tenants/{tenant_id}/test-github`
- `GET /api/admin/runs`
- `GET /api/admin/runs/{run_id}`

All admin and run lookup endpoints use HTTP Basic auth with:
- username: `ORCHESTRATOR_ADMIN_USERNAME`
- password: `ORCHESTRATOR_ADMIN_PASSWORD`

## CLI entrypoints
```bash
python -m orchestrator migrate
python -m orchestrator worker
python -m orchestrator run --tenant TENANT_ID --issue MAB-123
python -m orchestrator poll --tenant all
python -m orchestrator poll --tenant TENANT_ID
```

Equivalent installed console script:
```bash
orchestrator migrate
```

## Run locally
Start API:
```bash
uvicorn orchestrator.api.main:app --reload --port 4000
```

Start worker:
```bash
python -m orchestrator worker
```

## Admin UI (Next.js + shadcn)
The admin UI lives in `admin-ui/` and runs separately from the API service.

Local UI dev:
```bash
cd admin-ui
npm install
npm run dev
```

Default UI URL: `http://localhost:4100`

## Docker
Build and run API + worker + admin UI:
```bash
docker compose up --build
```

API is exposed on `http://localhost:4000`.
Admin UI is exposed on `http://localhost:4100`.

## Tenant onboarding
1. Create a tenant via `POST /api/admin/tenants`.
2. Configure Jira fields:
   - `mcp_endpoint`
   - `auth_ref`
   - `project_keys`
   - `ready_label`
   - `ready_jql`
3. Configure GitHub App fields:
   - `app_id_ref`
   - `private_key_ref`
   - `installation_id`
4. Configure allowed repositories under `repos.allowlist`.
5. Validate connections:
   - `POST /api/admin/tenants/{tenant_id}/test-jira`
   - `POST /api/admin/tenants/{tenant_id}/test-github`

## GitHub App setup (tenant config)
Each tenant uses GitHub App mode (`mode=github_app`) with secret references:
- `app_id_ref` must resolve to your GitHub App ID
- `private_key_ref` must resolve to your GitHub App private key PEM
- `installation_id` must be the installation for the tenant repos

The service enforces tenant repo allowlists before clone/push/PR actions.

## Jira webhook setup
Point Jira webhook to:
```text
POST /jira/webhook/{tenant_id}
```

If tenant webhook auth is configured:
- set `jira.webhook_secret_ref` to an environment variable name
- send token via `X-Webhook-Token` or `Authorization: Bearer <token>`

Only issues containing the tenant `ready_label` are enqueued.

## End-to-end local flow
1. Apply migrations:
   - `python -m orchestrator migrate`
2. Create tenant via Admin API.
3. Send Jira webhook payload with `ready_label`.
4. Confirm run created:
   - `GET /api/admin/runs?tenant_id=...`
   - or `GET /runs/{run_id}`
5. Use manual queue command when needed:
   - `python -m orchestrator run --tenant TENANT_ID --issue MAB-123`

## Tests
```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Lint
```bash
ruff check .
```

## Project structure
- `orchestrator/api`: FastAPI routes and schemas.
- `orchestrator/core`: workflow logic, policy/guardrail utilities, security.
- `orchestrator/storage`: SQLAlchemy models and migrations.
- `orchestrator/tools`: Jira/GitHub/git/bootstrap integrations.
- `orchestrator/worker.py`: worker process loop.
