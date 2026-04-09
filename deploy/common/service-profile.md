# Production Service Profile

## Required Services (Default)
- `api`
- `admin-ui`
- `run-worker`
- `webhook-worker`
- `project-automation`
- `knowledge-sync`
- `discord-gateway`
- `discord-live-voice`
- `llama-cpp`
- `postgres`
- `redis`

## Excluded By Default In Production
- `mailpit`
- `tailscale`

## Health Requirements
- API endpoint `/health` must return healthy status.
- All background worker processes must be alive and restartable.
- Database migrations must complete before runtime is marked ready.

## Persistence Requirements
- Database data must be persisted.
- Redis data persistence must be configured where supported.
- Codex home and voice model caches must persist across restarts.
