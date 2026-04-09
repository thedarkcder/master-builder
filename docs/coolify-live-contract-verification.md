# Coolify Live Contract Verification

This repository now includes a standalone verifier for the live Coolify API contract:

`scripts/verify_coolify_contract.py`

It performs a temporary create/update/deploy/status cycle against an existing Coolify instance, then deletes the temporary application by default.

## What it checks

- Coolify project lookup
- Coolify environment lookup
- Coolify server lookup
- Temporary application create
- Application read after create
- Application update and read-back
- Bulk environment update and list-back
- Deployment start
- Deployment status polling
- Deployment history lookup
- Cleanup of the temporary application

## Required env vars

Set these before running the script:

- `COOLIFY_VERIFY_BASE_URL`
- `COOLIFY_VERIFY_API_TOKEN`
- `COOLIFY_VERIFY_PROJECT_UUID`
- `COOLIFY_VERIFY_SERVER_UUID`
- `COOLIFY_VERIFY_DESTINATION_UUID`
- `COOLIFY_VERIFY_REPOSITORY`

Optional env vars:

- `COOLIFY_VERIFY_BRANCH` defaults to `main`
- `COOLIFY_VERIFY_ENVIRONMENT_NAME` defaults to `production`
- `COOLIFY_VERIFY_BUILD_PACK` defaults to `dockerfile`
- `COOLIFY_VERIFY_APP_NAME` overrides the temporary application name
- `COOLIFY_VERIFY_TIMEOUT_SECONDS` defaults to `600`
- `COOLIFY_VERIFY_POLL_INTERVAL_SECONDS` defaults to `5`
- `COOLIFY_VERIFY_FORCE_DEPLOY` set to `1` to force rebuild
- `COOLIFY_VERIFY_INSTANT_DEPLOY` set to `1` to skip queueing

## Guardrails

- The script refuses to run unless `--execute` is passed.
- Missing required env vars fail closed before any API call.
- The temporary application name is generated automatically unless you override it explicitly.
- Cleanup runs by default and removes configurations, volumes, connected networks, and Docker artifacts for the temporary app.
- Use `--keep-application` only when you want to inspect the temporary resource after a failure.

## Example

```bash
export COOLIFY_VERIFY_BASE_URL="https://coolify.example.com/api/v1"
export COOLIFY_VERIFY_API_TOKEN="***"
export COOLIFY_VERIFY_PROJECT_UUID="project-uuid"
export COOLIFY_VERIFY_SERVER_UUID="server-uuid"
export COOLIFY_VERIFY_DESTINATION_UUID="destination-uuid"
export COOLIFY_VERIFY_REPOSITORY="https://github.com/example/repo"

python scripts/verify_coolify_contract.py --execute
```

## Expected output

The script prints one line per check in the form:

```text
[PASS] application create: created temporary application ...
[FAIL] deployment status: final status failed
```

It also prints a JSON summary as the final line so the command can be machine-parsed in CI or a release runbook.

## Notes

- The verifier assumes the repository is reachable by the target Coolify instance.
- It does not create or delete the Coolify project, server, or destination.
- If deployment status never reaches a terminal state before the timeout, the script fails and reports the last observed status.
