# Production Service Profile

Status: intended production topology and validation requirements, not evidence that a provider package has passed them. Enable optional workers only after their prerequisites and integration verification; see [the support matrix](../../docs/support-matrix.md).

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
- `clickhouse`

## Excluded By Default In Production
- `mailpit`
- `tailscale`

## Health Requirements
- API endpoint `/health` must return healthy status.
- All background worker processes must be alive and restartable.
- Database migrations must complete before runtime is marked ready.
- ClickHouse must accept authenticated writes before API startup is marked ready.

## Persistence Requirements
- Database data must be persisted.
- ClickHouse data must be persisted.
- Codex home and voice model caches must persist across restarts.
