# Required Environment Contract

This file defines the minimum runtime environment contract for production deployments.

## Required Across Providers
- `ORCHESTRATOR_ADMIN_USERNAME`
- `ORCHESTRATOR_ADMIN_PASSWORD`
- `ORCHESTRATOR_PUBLIC_API_BASE_URL`
- `ORCHESTRATOR_ADMIN_UI_BASE_URL`
- `ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET`
- `ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET`
- `ORCHESTRATOR_SECRET_CRYPTO_PROVIDER`
- `ORCHESTRATOR_DATABASE_URL`
- `ORCHESTRATOR_REDIS_URL`

If `ORCHESTRATOR_SECRET_CRYPTO_PROVIDER=vault_transit`:
- `ORCHESTRATOR_VAULT_ADDR`
- `ORCHESTRATOR_VAULT_TOKEN`
- `ORCHESTRATOR_VAULT_TRANSIT_KEY`

If `ORCHESTRATOR_SECRET_CRYPTO_PROVIDER=aws_kms`:
- `ORCHESTRATOR_AWS_KMS_REGION`
- `ORCHESTRATOR_AWS_KMS_KEY_ID`

If `ORCHESTRATOR_SECRET_CRYPTO_PROVIDER=gcp_kms`:
- `ORCHESTRATOR_GCP_KMS_KEY_NAME`

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
- Uses local database and redis services in the same deployment stack.
- `ORCHESTRATOR_DATABASE_URL` and `ORCHESTRATOR_REDIS_URL` should point to internal service names.

### AWS
- Uses managed HA data services.
- `ORCHESTRATOR_DATABASE_URL` must point to RDS endpoint.
- `ORCHESTRATOR_REDIS_URL` must point to ElastiCache endpoint.
- Secret values should be sourced from Secrets Manager at deploy time.
- Secret crypto can be backed by AWS KMS when `ORCHESTRATOR_SECRET_CRYPTO_PROVIDER=aws_kms`.

### GCP
- Uses managed HA data services.
- `ORCHESTRATOR_DATABASE_URL` must point to Cloud SQL connectivity endpoint.
- `ORCHESTRATOR_REDIS_URL` must point to Memorystore endpoint.
- Secret values should be sourced from Secret Manager at deploy time.
- Secret crypto can be backed by Cloud KMS when `ORCHESTRATOR_SECRET_CRYPTO_PROVIDER=gcp_kms`.
