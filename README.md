# master-builder

Multi-tenant agent orchestrator service scaffold.

## Requirements
- Python 3.11+

## Quick start
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

Set admin auth and database settings:
```bash
export ORCHESTRATOR_ADMIN_USERNAME=admin
export ORCHESTRATOR_ADMIN_PASSWORD=change-me
export ORCHESTRATOR_DATABASE_URL=sqlite:///./orchestrator.db
```

## Run API
```bash
uvicorn orchestrator.api.main:app --reload
```

## Key endpoints
- `GET /health`
- `POST /jira/webhook/{tenant_id}`
- `GET /api/admin/tenants` (HTTP Basic auth)
- `POST /api/admin/tenants`
- `DELETE /api/admin/tenants/{tenant_id}`
- `GET /api/admin/runs`

## Core module structure
- `orchestrator/api`: FastAPI app factory, admin routes, and webhook ingestion.
- `orchestrator/core`: configuration loading, logging setup, and shared security helpers.
- `orchestrator/storage`: SQLAlchemy models, DB session factory, and Alembic migrations.
- `orchestrator/tools`: external integration adapters (Jira MCP, GitHub, Discord).
- `orchestrator/worker.py`: background worker entrypoint and lifecycle wiring.

SQLite is the default local backend (`ORCHESTRATOR_DATABASE_URL`), and schema evolution is managed by Alembic so the service can migrate cleanly to Postgres by switching the database URL and applying migrations.

## Apply migrations
```bash
python -m orchestrator migrate
```

## Run worker
```bash
python -m orchestrator worker
```

## Tests
```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```
