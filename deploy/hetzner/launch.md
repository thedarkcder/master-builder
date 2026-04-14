# Hetzner One-Click Launch

This package targets a non-HA single-host deployment.

## Current One-Click Entry
- Use Hetzner user-data (cloud-init) to execute `deploy/hetzner/bootstrap.sh` on first boot.
- This gives one launch action from server creation to running stack.
- Use `deploy/hetzner/launch.sh` for a single command launcher via Hetzner API.
- External-user tutorial parent route: `/tutorials`.
- External-user setup route: `/tutorials/hetzner-setup`.

## Setup Route
Use the app route to collect inputs and generate both artifacts:
- launch command for remote install (`curl ... | bash`)
- cloud-init YAML for Hetzner Console server creation

## Required User-Data Inputs
Provide these environment variables before running the bootstrap:
- `ORCHESTRATOR_ADMIN_PASSWORD`
- `ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET`
- `ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET`
- `ORCHESTRATOR_SECRET_CRYPTO_PROVIDER`
- `NEXTAUTH_SECRET`
- `ORCHESTRATOR_EMAIL_FROM_ADDRESS`

Optional:
- `ORCHESTRATOR_VAULT_ADDR`, `ORCHESTRATOR_VAULT_TOKEN`, `ORCHESTRATOR_VAULT_NAMESPACE`, `ORCHESTRATOR_VAULT_TRANSIT_KEY` when `ORCHESTRATOR_SECRET_CRYPTO_PROVIDER=vault_transit`
- `ORCHESTRATOR_AWS_KMS_REGION`, `ORCHESTRATOR_AWS_KMS_KEY_ID` when `ORCHESTRATOR_SECRET_CRYPTO_PROVIDER=aws_kms`
- `ORCHESTRATOR_GCP_KMS_KEY_NAME` when `ORCHESTRATOR_SECRET_CRYPTO_PROVIDER=gcp_kms`
- `ORCHESTRATOR_SECRETS_ENCRYPTION_KEY` when `ORCHESTRATOR_SECRET_CRYPTO_PROVIDER=fernet_legacy`
- `ORCHESTRATOR_RESEND_API_KEY` (required if using `resend`)
- `REPO_URL` (defaults to `https://github.com/your-org/master-builder.git`)
- `REPO_REF` (defaults to `main`)

## Cloud-Init Example
```yaml
#cloud-config
package_update: true
runcmd:
  - export ORCHESTRATOR_ADMIN_PASSWORD='replace-me'
  - export ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET='replace-me'
  - export ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET='replace-me'
  - export ORCHESTRATOR_SECRET_CRYPTO_PROVIDER='vault_transit'
  - export ORCHESTRATOR_VAULT_ADDR='https://vault.example.com'
  - export ORCHESTRATOR_VAULT_TOKEN='replace-me'
  - export ORCHESTRATOR_VAULT_TRANSIT_KEY='master-builder'
  - export NEXTAUTH_SECRET='replace-me'
  - export ORCHESTRATOR_EMAIL_FROM_ADDRESS='no-reply@example.com'
  - export ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER='resend'
  - export ORCHESTRATOR_RESEND_API_KEY='replace-me'
  - git clone https://github.com/your-org/master-builder.git /opt/master-builder-bootstrap
  - bash /opt/master-builder-bootstrap/deploy/hetzner/bootstrap.sh
```

## Deploy Button Wiring
When the Hetzner app slug is registered, publish this button URL:
- `https://console.hetzner.com/deploy/<app-slug>`

Until the slug exists, the cloud-init path above is the one-click installation method.

## Single-Command Launch
```bash
bash deploy/hetzner/launch.sh
```

The script now prompts for all missing required values interactively.
You can still pre-export any values to skip prompts.
