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
export ORCHESTRATOR_DATABASE_URL=postgresql+psycopg://orchestrator:orchestrator@localhost:4402/orchestrator
export ORCHESTRATOR_CORS_ORIGINS=http://localhost:4100,http://127.0.0.1:4100
export ORCHESTRATOR_ADMIN_UI_BASE_URL=http://localhost:4100
export ORCHESTRATOR_PUBLIC_API_BASE_URL=http://localhost:4000
export ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER=smtp
export ORCHESTRATOR_EMAIL_FROM_ADDRESS=no-reply@masterbuilder.local
export ORCHESTRATOR_SMTP_HOST=localhost
export ORCHESTRATOR_SMTP_PORT=1025
export ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET=change-me
export ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET=change-me
export ORCHESTRATOR_CODEX_CLI_COMMAND=codex
export ORCHESTRATOR_CODEX_MODEL=gpt-5-codex
export ORCHESTRATOR_CODEX_STDERR_LOG_MODE=errors_only # all|errors_only|off
export ORCHESTRATOR_CODEX_PERSIST_TURN_COMPLETED_USAGE=true
export ORCHESTRATOR_WORKER_POLL_INTERVAL_SECONDS=5
export ORCHESTRATOR_VOICE_REPLY_PROVIDER=pocket_tts
# Optional fallback voice if room/persona config does not supply one.
export ORCHESTRATOR_POCKET_TTS_VOICE=alba
export ORCHESTRATOR_SECRETS_ENCRYPTION_KEY=$(python - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
)

# Store OAuth secret values via managed secrets API/UI, not shell exports:
# - secret ref JIRA_OAUTH_CLIENT_ID -> Jira OAuth client id
# - secret ref JIRA_OAUTH_CLIENT_SECRET -> Jira OAuth client secret

```

Worker and Discord `/ask` now use native Codex CLI auth (not `OPENAI_API_KEY`).
For containers, run one-time login and keep the shared Codex auth volume:
```bash
docker compose run --rm run-worker codex login --device-auth
```

## Jira release-train automation (repo-level)
This repo uses two release-train workflows:
- a fast label sync loop for `Ready to Release` issues
- a close loop that waits checks, merges release PRs, and closes Jira issues

Workflows:
- `.github/workflows/release-train-sync.yml`
  - runs every 30 minutes (and manual dispatch)
  - assigns `release:vX.Y.Z` labels to `READY TO RELEASE` issues
- `.github/workflows/release-train-close.yml`
  - runs hourly (plus release publish/manual dispatch)
  - finds `release:vX.Y.Z` issues, waits for required checks, merges matching PRs to `main`, then transitions issues to `Done`

Enable with:
- Repository variable: `ENABLE_JIRA_RELEASE_AUTOMATION=true`
- Variables:
  - `RELEASE_JIRA_BASE_URL`
  - optional `RELEASE_JIRA_CLOUD_ID` (required for scoped Atlassian API tokens)
  - `RELEASE_JIRA_EMAIL`
  - `RELEASE_JIRA_PROJECT_KEY`
  - optional `RELEASE_DONE_STATUS` (default `Done`)
- Secret:
  - `RELEASE_JIRA_API_TOKEN`

Detailed setup:
- `docs/release-train-automation.md`
- `docs/run-decision-engine.md` (where run queue/start decisions are made)

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
- `GET /api/admin/secrets`
- `PUT /api/admin/secrets/{secret_ref}`
- `POST /api/admin/secrets/resolve`

All admin and run lookup endpoints use HTTP Basic auth with:
- username: `ORCHESTRATOR_ADMIN_USERNAME`
- password: `ORCHESTRATOR_ADMIN_PASSWORD`

## Managed secrets
The orchestrator now supports an encrypted managed secret store (database-backed) with environment fallback:
- store/update refs via admin API/UI without restarting containers
- resolve refs at runtime for Jira OAuth, GitHub App credentials, and webhook secrets
- list endpoints never return plaintext values

Example upsert:
```bash
AUTH_HEADER="Authorization: Basic $(printf '%s:%s' \"$ORCHESTRATOR_ADMIN_USERNAME\" \"$ORCHESTRATOR_ADMIN_PASSWORD\" | base64)"
curl \
  -X PUT \
  -H "$AUTH_HEADER" \
  -H 'Content-Type: application/json' \
  -d '{"value":"12345"}' \
  http://localhost:4000/api/admin/secrets/MB_GH_APP_ID
```

Example resolve check:
```bash
AUTH_HEADER="Authorization: Basic $(printf '%s:%s' \"$ORCHESTRATOR_ADMIN_USERNAME\" \"$ORCHESTRATOR_ADMIN_PASSWORD\" | base64)"
curl \
  -X POST \
  -H "$AUTH_HEADER" \
  -H 'Content-Type: application/json' \
  -d '{"secret_ref":"MB_GH_APP_ID"}' \
  http://localhost:4000/api/admin/secrets/resolve
```

## CLI entrypoints
```bash
python -m orchestrator migrate
python -m orchestrator worker-runs
python -m orchestrator worker-webhooks
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

Start run worker (processes queued runs using Codex-backed PM/Dev/Test/Review agents):
```bash
python -m orchestrator worker-runs
```

Start webhook worker (processes queued Jira/GitHub/Discord webhook jobs):
```bash
python -m orchestrator worker-webhooks
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
Login route: `http://localhost:4100/login`

UI sections:
- `/tenants` for list and health checks
- `/tenants/new` for wizard-based tenant setup (Jira connect + GitHub install)
- `/tenants/{tenant_id}/edit` for structured tenant update form
- `/runs` for run observability

## Docker
Build and run API + worker + Postgres + optional admin UI + tailscale sidecar:
```bash
cp .env.example .env
docker compose up --build
```

API is exposed on `http://localhost:4000`.
Postgres is exposed on `localhost:4402`.
Mailpit SMTP is exposed on `localhost:1025`.
Mailpit inbox UI is exposed on `http://localhost:8025`.
Admin UI (if running locally) is exposed on `http://localhost:4100`.
Tailscale sidecar uses `TS_AUTHKEY` from your environment (required for tailnet auth).

For host-based API development, you can run only the local mail sink:
```bash
docker compose up -d mailpit
```
Then keep `ORCHESTRATOR_SMTP_HOST=localhost` and `ORCHESTRATOR_SMTP_PORT=1025` so invite and onboarding emails land in Mailpit instead of a real provider.

### Worker build toolchains
The worker image now includes:
- Java 17 JDK
- Android SDK command-line tools (`platform-tools`, `build-tools;34.0.0`, `platforms;android-34`)
- Node/npm, `ripgrep`, and Codex CLI

Quick checks:
```bash
docker compose run --rm run-worker java -version
docker compose run --rm run-worker sdkmanager --version
```

Swift/iOS note:
- `xcodebuild` (iOS/macOS builds) cannot run in this Linux worker container.
- Use a macOS self-hosted runner/container host for iOS build/test steps.

### Tailscale Funnel URL
Start API and Tailscale sidecar:
```bash
docker compose up -d api tailscale
```

Connect and verify:
```bash
docker exec -it master-builder-tailscale tailscale status
```

Expose API publicly on Funnel:
```bash
docker exec -it master-builder-tailscale tailscale funnel --bg 4000
docker exec -it master-builder-tailscale tailscale funnel status
```

Use the returned `https://<device>.<tailnet>.ts.net` URL for external callbacks (Jira/GitHub/Discord) during local testing.

If your admin UI is hosted on Vercel, use this Funnel URL for API callbacks only.

## Tenant onboarding
1. Create a tenant via `POST /api/admin/tenants`.
2. Connect Jira from the wizard (`Connect Jira`) and select `project_keys`.
3. Connect GitHub integration from the wizard (`Install GitHub App`) so `installation_id` is saved automatically.
4. Set tenant repository under `repos.github_repository`.
5. Validate connections:
   - `POST /api/admin/tenants/{tenant_id}/test-jira`
   - `POST /api/admin/tenants/{tenant_id}/test-github`

## Discord app setup
Configure one Discord app (bot) and install it into your server. The same app can be reused across tenants.

1. Create app + bot
   - Open Discord Developer Portal: `https://discord.com/developers/applications`
   - Create a new application
   - Go to `Bot` and click `Add Bot`
   - Copy and store bot token securely (do not commit it)

2. Enable intents
   - In `Bot` settings, enable:
     - `SERVER MEMBERS INTENT`
     - `MESSAGE CONTENT INTENT` (required for prefix commands like `!status`)

3. Configure OAuth install
   - In `OAuth2 > URL Generator`:
     - Scopes: `bot` (and `applications.commands` if you later add slash commands)
     - Bot permissions:
       - `View Channels`
       - `Send Messages`
       - `Read Message History`
       - `Manage Channels` (required for automatic tenant channel create/reuse)
   - Open generated invite URL and install bot into your target Discord server

4. Capture IDs (Developer Mode must be enabled in Discord client)
   - Server ID (`guild_id`): right-click server -> `Copy Server ID`
   - User ID for command allowlist: right-click user -> `Copy User ID`

5. Configure backend globals
   - Set `ORCHESTRATOR_DISCORD_GUILD_ID` to your server ID, or store it in Secrets Manager as `DISCORD_GUILD_ID`.
   - Optional: set `ORCHESTRATOR_DISCORD_CHANNEL_NAME_TEMPLATE` (default `tenant-{tenant_id}`).
   - Optional: set `ORCHESTRATOR_DISCORD_CHANNEL_CATEGORY_ID` to place channels under a category.
   - Store bot token in Secrets Manager under `DISCORD_BOT_TOKEN` (or change `ORCHESTRATOR_DISCORD_BOT_TOKEN_SECRET_REF`).
   - Store Discord interactions public key in Secrets Manager under `DISCORD_INTERACTIONS_PUBLIC_KEY`.

6. Configure Discord Interactions callback
   - In Discord Developer Portal -> your app -> `General Information` copy `Public Key`.
   - Save it as managed secret `DISCORD_INTERACTIONS_PUBLIC_KEY`.
   - In Discord Developer Portal -> `Interactions Endpoint URL`, set:
     - `https://<your-api-domain>/discord/interactions`

7. Configure tenant in admin UI
   - Enable Discord settings for tenant.
   - Set `notify_events` checkboxes.
   - Save tenant: backend auto-creates/reuses tenant channel and stores `channel_id`.

Notes:
- Server ID and channel ID are different values.
- Native interactions endpoint is `POST /discord/interactions`.
- Internal command API endpoint is `POST /discord/command/{tenant_id}`.
- Slash commands are auto-synced to the configured guild on API startup (best-effort).
- Command set includes `/ask` for board questions (`!ask` in internal command format).
- Automatic channel create/reuse requires `Manage Channels` permission.
8. Inspect repo bootstrap state:
   - `GET /api/admin/tenants/{tenant_id}/repo-bootstrap`

## GitHub App setup
GitHub App credentials resolve with scoped fallback in this order:
1. `project/{tenant_id}/{project_id}/{ref}`
2. `tenant/{tenant_id}/{ref}`
3. `platform/{ref}`
4. `{ref}` (legacy direct ref / env var)

Default refs:
- secret ref `GITHUB_APP_SLUG` (GitHub App slug)
- `ORCHESTRATOR_GITHUB_APP_ID_REF` (defaults to `GITHUB_APP_ID`)
- `ORCHESTRATOR_GITHUB_PRIVATE_KEY_REF` (defaults to `GITHUB_APP_PRIVATE_KEY`)

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

GitHub App webhook URL:
- `POST /github/webhook`

Recommended webhook signature verification setup:
- set `ORCHESTRATOR_GITHUB_WEBHOOK_SECRET_REF` to the secret-ref key name (example: `secret/github-webhook`)
- set the matching environment variable to the raw GitHub App webhook secret value
- configure the same raw secret in GitHub App webhook settings

## Jira webhook setup
Point Jira webhook to:
```text
POST /jira/webhook/{tenant_id}
```

If tenant webhook auth is configured:
- set `jira.webhook_secret_ref` to an environment variable name
- send token via `X-Webhook-Token` or `Authorization: Bearer <token>`

Only issues in tenant `ready_statuses` are enqueued (default: `Ready for Agent`).
Issues in done status categories are never enqueued.
Issue status is not auto-transitioned when a run starts.
Status transitions into a ready status are treated as primary triggers; updates while already ready are rechecked idempotently.

## End-to-end local flow
1. Apply migrations:
   - `python -m orchestrator migrate`
2. Create tenant via Admin API.
3. Send Jira webhook payload with issue status set to a configured ready status (for example `Ready for Agent`).
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
pytest -q
```

## Coverage
```bash
pytest -q --cov=orchestrator --cov-report=term-missing --cov-report=xml
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
