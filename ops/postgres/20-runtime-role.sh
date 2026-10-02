#!/bin/sh
# Explicit owner-side provisioning. Existing roles are never silently reused.
set -eu
: "${POSTGRES_USER:?Set the migration owner role}"
: "${POSTGRES_DB:?Set the database name}"
: "${POSTGRES_RUNTIME_PASSWORD:?Set the independent runtime password}"
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 \
  --set=runtime_password="$POSTGRES_RUNTIME_PASSWORD" --set=migration_owner="$POSTGRES_USER" <<'SQL'
SELECT format('CREATE ROLE orchestrator_runtime LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD %L', :'runtime_password') \gexec
GRANT USAGE ON SCHEMA public TO orchestrator_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO orchestrator_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO orchestrator_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_owner" IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO orchestrator_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_owner" IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO orchestrator_runtime;
SQL
