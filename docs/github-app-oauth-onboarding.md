# GitHub App Creation and OAuth-Style Onboarding

This document describes the tenant onboarding model for GitHub integration.

## Current state (implemented today)
- Tenant onboarding uses a wizard flow in the admin UI.
- GitHub setup is install-button based:
  - click `Install GitHub App`
  - complete install on GitHub
  - callback persists `installation_id` on tenant config
- GitHub App credentials are server-managed (`ORCHESTRATOR_GITHUB_APP_ID_REF`, `ORCHESTRATOR_GITHUB_PRIVATE_KEY_REF`).
- Secret refs can be managed in admin UI at `/secrets` (encrypted-at-rest, no restart required).

## Jira + GitHub flow summary
- Step 1: tenant basics
- Step 2: connect Jira OAuth and choose project keys
- Step 3: connect GitHub App installation
- Step 4: map repos and policy
- Step 5: review and save

## Do we need to create the GitHub App ourselves?
Yes. You must create a GitHub App (or use an existing app owned by your org) and install it on the target repositories.

## GitHub App creation checklist
1. In GitHub, go to `Settings -> Developer settings -> GitHub Apps -> New GitHub App`.
2. Set app name, homepage URL, callback URL, and webhook URL:
   - Callback URL: `GET /api/admin/github/install/callback` on your public API base URL
   - Webhook URL: `POST /github/webhook` on your public API base URL
3. Generate a private key and store it in your secret manager.
4. Record the App ID.
5. Install the app to repos the tenant is allowed to use.
6. Verify the install button callback sets `installation_id` during wizard/edit flow.

## Recommended minimum app permissions
- Repository `Contents`: Read and write (branch + commits)
- Repository `Pull requests`: Read and write (open/update PRs)
- Repository `Metadata`: Read
- Repository `Checks`: Read
- Repository `Commit statuses`: Read

If webhook processing is enabled for GitHub events, also configure webhook delivery and secret handling.

## Webhook secret handling
- Configure one shared GitHub App webhook secret in GitHub.
- In orchestrator runtime, set:
  - `ORCHESTRATOR_GITHUB_WEBHOOK_SECRET_REF` (example value: `secret/github-webhook`)
  - environment variable keyed by that ref containing the raw secret.
- The API validates `X-Hub-Signature-256` for inbound GitHub webhook deliveries.

## OAuth-style install/connect flow
1. Operator creates a tenant with basic tenant/Jira fields.
2. Wizard step `Connect GitHub` shows button: `Install GitHub App`.
3. Clicking button redirects to GitHub App installation URL with signed `state` containing tenant context.
4. After approval, callback endpoint validates `state` and captures:
   - `installation_id`
   - installation target (org/user)
   - repository selection (all or selected)
5. Service persists tenant GitHub integration details.
6. Wizard continues to repo mapping and test connection.
7. Tenant is marked ready only when GitHub + Jira checks pass.

## Data that should be saved from install callback
- `installation_id`
- installation account login/type
- selected repositories (or marker for all repos)
- webhook secret ref (if configured during onboarding)
- timestamp + actor metadata for audit trail
