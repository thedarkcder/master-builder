# master-builder

Multi-tenant Jira-driven agent orchestrator service.

## Requirements
- Python 3.11+
- Atlassian OAuth app credentials for Jira connect flow

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
export ORCHESTRATOR_CORS_ORIGINS=http://localhost:4080,http://127.0.0.1:4080
export ORCHESTRATOR_ADMIN_UI_BASE_URL=http://localhost:4080
export ORCHESTRATOR_PUBLIC_API_BASE_URL=http://localhost:4080
export ORCHESTRATOR_GITHUB_APP_SLUG=your-github-app-slug
export ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET=change-me
export ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET=change-me
export ORCHESTRATOR_SECRETS_ENCRYPTION_KEY=$(python - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
)

export SECRET_JIRA_CLIENT_ID=your-atlassian-oauth-client-id
export SECRET_JIRA_CLIENT_SECRET=your-atlassian-oauth-client-secret
export ORCHESTRATOR_JIRA_OAUTH_CLIENT_ID_REF=SECRET_JIRA_CLIENT_ID
export ORCHESTRATOR_JIRA_OAUTH_CLIENT_SECRET_REF=SECRET_JIRA_CLIENT_SECRET
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

Default ingress URL: `http://localhost:4080`
Login route via ingress: `http://localhost:4080/login`

UI sections:
- `/tenants` for list and health checks
- `/tenants/new` for wizard-based tenant setup (Jira connect + GitHub install)
- `/tenants/{tenant_id}/edit` for structured tenant update form
- `/runs` for run observability

## Docker
Build and run API + worker + admin UI + reverse proxy + cloudflared:
```bash
docker compose up --build
```

Primary local ingress is `http://localhost:4080`.
- API through ingress: `http://localhost:4080/health`, `http://localhost:4080/api/*`
- Admin UI through ingress: `http://localhost:4080/login`

Direct service ports are still exposed for troubleshooting:
- API direct: `http://localhost:4000`
- Admin UI direct: `http://localhost:4100`

### Quick tunnel URL (trycloudflare)
The stack includes `cloudflared` in Quick Tunnel mode, targeting the reverse proxy.

Watch logs and copy the generated public URL:
```bash
docker compose logs -f cloudflared
```

Extract just the URL:
```bash
docker compose logs cloudflared | rg -o \"https://[-a-z0-9]+\\.trycloudflare\\.com\" | tail -n 1
```

Use that URL for external callbacks (Jira/GitHub/Discord) during local testing.

Detailed route matrix and verification steps:
- `docs/local-reverse-proxy.md`

## Tenant onboarding
1. Create a tenant via `POST /api/admin/tenants`.
2. Connect Jira from the wizard (`Connect Jira`) and select `project_keys`.
3. Connect GitHub integration from the wizard (`Install GitHub App`) so `installation_id` is saved automatically.
4. Configure allowed repositories under `repos.allowlist`.
5. Validate connections:
   - `POST /api/admin/tenants/{tenant_id}/test-jira`
   - `POST /api/admin/tenants/{tenant_id}/test-github`
6. Inspect repo bootstrap state:
   - `GET /api/admin/tenants/{tenant_id}/repo-bootstrap`

## GitHub App setup
The service uses one server-managed GitHub App for all tenants:
- `ORCHESTRATOR_GITHUB_APP_SLUG`
- `ORCHESTRATOR_GITHUB_APP_ID_REF` (defaults to `secret/app-id`)
- `ORCHESTRATOR_GITHUB_PRIVATE_KEY_REF` (defaults to `secret/private-key`)

Tenants only store GitHub mode + installation state (`installation_id`).

The service enforces tenant repo allowlists before clone/push/PR actions.

## GitHub App creation and OAuth onboarding direction
Yes, you need to create a GitHub App (or use one already owned by your org) and install it on target repos.

The full creation + onboarding direction is documented here:
- `docs/github-app-oauth-onboarding.md`

Short version:
- Create one org-level GitHub App with required repo permissions.
- Configure app ID/private key in server-managed secret refs.
- In tenant setup:
  - create tenant basics
  - click `Connect Jira`
  - click `Install GitHub App`
  - approve install on GitHub
  - continue wizard to repo mapping and webhook checks

GitHub App setup callback URL:
- `GET /api/admin/github/install/callback`

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

## Repo bootstrap
Canonical bootstrap utilities now ensure repo-level `.codex` assets exist:
- `.codex/OPERATING.md`
- `.codex/POLICY.md`
- `.codex/skills/run_tests.md`
- `.codex/skills/add_tests.md`
- `.codex/skills/pr_checklist.md`
- `.codex/skills/security_sanity.md`
- optional `AGENTS.md` at repo root

Bootstrap persistence is tracked per tenant/repo in `repo_bootstrap_states`.

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
