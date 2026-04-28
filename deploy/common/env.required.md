# Required Environment Contract

This file defines the minimum runtime environment contract for production deployments.

## Required Across Providers
- `ORCHESTRATOR_ADMIN_USERNAME`
- `ORCHESTRATOR_ADMIN_PASSWORD`
- `ORCHESTRATOR_PUBLIC_API_BASE_URL`
- `ORCHESTRATOR_ADMIN_UI_BASE_URL`
- `ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET`
- `ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET`
- `ORCHESTRATOR_SECRETS_ENCRYPTION_KEY`
- `ORCHESTRATOR_DATABASE_URL`
- `ORCHESTRATOR_CLICKHOUSE_HTTP_URL`
- `ORCHESTRATOR_CLICKHOUSE_DATABASE`
- `ORCHESTRATOR_CLICKHOUSE_USERNAME`
- `ORCHESTRATOR_CLICKHOUSE_PASSWORD`

## Required For Email Delivery
- `ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER`
- `ORCHESTRATOR_EMAIL_FROM_ADDRESS`

If `ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER=smtp`:
- `ORCHESTRATOR_SMTP_HOST`
- `ORCHESTRATOR_SMTP_PORT`

If `ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER=resend`:
- `ORCHESTRATOR_RESEND_API_KEY`

## Required For Voice Runtime
- `ORCHESTRATOR_VOICE_STT_PROVIDER`
- `ORCHESTRATOR_VOICE_TTS_PROVIDER`

## Provider Notes

### Hetzner
- Uses local Postgres and ClickHouse services in the same deployment stack.
- `ORCHESTRATOR_DATABASE_URL` and `ORCHESTRATOR_CLICKHOUSE_HTTP_URL` should point to internal service names.

### AWS
- Uses managed HA data services.
- `ORCHESTRATOR_DATABASE_URL` must point to RDS endpoint.
- `ORCHESTRATOR_CLICKHOUSE_HTTP_URL` must point to the approved ClickHouse endpoint.
- Secret values should be sourced from Secrets Manager at deploy time.

### GCP
- Uses managed HA data services.
- `ORCHESTRATOR_DATABASE_URL` must point to Cloud SQL connectivity endpoint.
- `ORCHESTRATOR_CLICKHOUSE_HTTP_URL` must point to the approved ClickHouse endpoint.
- Secret values should be sourced from Secret Manager at deploy time.
