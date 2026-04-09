# Internal Coolify Environment Wiring Runbook

Use `scripts/wire_internal_coolify_environment.py` to wire tenant-specific Coolify secrets and deployment callbacks.

Run the flow once per environment (`dev`, `stage`, `prod`). Do not reuse tokens between environments.

## Required inputs per environment

- `tenant_id`
- Orchestrator admin API base URL, for example `https://orchestrator-dev.example.com`
- Public API base URL for webhook callbacks (defaults to admin base URL if omitted)
- Coolify API base URL, for example `https://coolify-dev.example.com/api/v1`
- Coolify `project_uuid`
- Coolify `server_uuid`
- Coolify `destination_uuid`
- Coolify API token (environment-specific)
- Coolify webhook token (environment-specific)

Optional metadata:

- `infrastructure_provider` (`aws` or `hetzner`)
- `region`, `base_domain`, `platform_subdomain`, `coolify_environment_name`
- Additional tenant secrets and deployment-plane secret ref mappings

## Endpoints used by the script

Orchestrator admin API:

- `GET /api/admin/tenants/{tenant_id}/deployment-plane`
- `PUT /api/admin/tenants/{tenant_id}/deployment-plane`
- `PUT /api/admin/tenants/{tenant_id}/secrets/{secret_key}`
- `GET /api/admin/tenants/{tenant_id}/projects`

Orchestrator webhook callback URL pattern:

- `POST /deployments/coolify/webhook/{tenant_id}/{project_id}/{token}`

## Recommended order

1. Export env-specific token values.
2. Run the script without `--execute` and inspect the dry-run output.
3. Re-run with `--execute` to persist secrets and deployment-plane config.
4. Copy generated webhook URLs into the corresponding Coolify projects.
5. Verify webhook jobs are arriving in orchestrator observability.

## Example (`dev`)

```bash
export MB_ADMIN_USERNAME=admin
export MB_ADMIN_PASSWORD='***'
export COOLIFY_API_TOKEN='***'
export COOLIFY_WEBHOOK_TOKEN='***'
export BACKUP_BUCKET_ENV='mb-backups-dev'

python scripts/wire_internal_coolify_environment.py \
  --api-base-url "https://orchestrator-dev.example.com" \
  --public-api-base-url "https://orchestrator-dev.example.com" \
  --tenant-id TENANT_ID \
  --infrastructure-provider aws \
  --region us-east-1 \
  --base-domain apps.example.com \
  --platform-subdomain builder \
  --coolify-api-base-url "https://coolify-dev.example.com/api/v1" \
  --coolify-project-uuid "project-uuid-dev" \
  --coolify-environment-name production \
  --coolify-server-uuid "server-uuid-dev" \
  --coolify-destination-uuid "destination-uuid-dev" \
  --tenant-secret-env BACKUP_BUCKET=BACKUP_BUCKET_ENV \
  --plane-secret-ref backup_bucket=BACKUP_BUCKET \
  --execute
```

Repeat for `stage` and `prod` using each environment’s own URLs, UUIDs, and token values.
