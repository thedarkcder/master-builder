# GitHub App Creation and OAuth-Style Onboarding

This document describes the desired tenant onboarding model for GitHub integration.

## Current state (implemented today)
- Tenant creation requires manual GitHub App references:
  - `app_id_ref`
  - `private_key_ref`
  - `installation_id`
- This works, but is operator-heavy and error-prone.

## Target state (requested)
- Tenant onboarding should be a wizard, not a single long form.
- GitHub setup should use an install button and callback flow, not manual copy/paste of installation details.
- After tenant creation, operator should click a GitHub install/connect button, authorize installation, and have details saved automatically.

## Do we need to create the GitHub App ourselves?
Yes. You must create a GitHub App (or use an existing app owned by your org) and install it on the target repositories.

## GitHub App creation checklist
1. In GitHub, go to `Settings -> Developer settings -> GitHub Apps -> New GitHub App`.
2. Set app name, homepage URL, and webhook URL (can be placeholder for local dev).
3. Generate a private key and store it in your secret manager.
4. Record the App ID.
5. Install the app to repos the tenant is allowed to use.
6. Record the installation ID (for current manual mode).

## Recommended minimum app permissions
- Repository `Contents`: Read and write (branch + commits)
- Repository `Pull requests`: Read and write (open/update PRs)
- Repository `Metadata`: Read
- Repository `Checks`: Read
- Repository `Commit statuses`: Read

If webhook processing is enabled for GitHub events, also configure webhook delivery and secret handling.

## OAuth-style install/connect flow (target behavior)
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

## Tenant setup wizard shape (target)
1. Tenant basics
2. Jira connection
3. GitHub install/connect
4. Repo mapping
5. Policy + limits
6. Review + save

## Data that should be saved from install callback
- `installation_id`
- installation account login/type
- selected repositories (or marker for all repos)
- webhook secret ref (if configured during onboarding)
- timestamp + actor metadata for audit trail

## Jira alignment
This direction should be tracked primarily in:
- `MAB-8` GitHub App auth, install/connect flow, and installation persistence
- `MAB-13` Admin UI wizard and auth UX
- `MAB-6` Tenant onboarding model updates for wizard flow
