# Jira Release-Train Automation

This repo supports Jira-driven release-train automation without manually setting `Fix Version`.

## Goal
- When an issue transitions to `Ready to Release`, it is automatically assigned a release label: `release:vX.Y.Z`.
- Release-train automation can merge matching PRs to `main` and transition Jira issues to `Done`.

## Workflows
- `.github/workflows/release-train-sync.yml`
  - Manual helper workflow.
  - Finds `Ready to Release` issues and assigns `release:vX.Y.Z`.
  - If no explicit version is passed, computes the next release from git tags.
- `.github/workflows/release-train-close.yml`
  - Runs hourly (and via manual dispatch).
  - Resolves target release version.
  - Assigns `Ready to Release` issues to `release:vX.Y.Z`.
  - Finds open PRs with matching Jira keys in PR title and merges them to `main`.
  - Transitions Jira issues labeled `release:vX.Y.Z` to `Done`.

## Required GitHub configuration
Set repository variable:
- `ENABLE_JIRA_RELEASE_AUTOMATION=true`

Set repository variables:
- `RELEASE_JIRA_BASE_URL` (for example `https://example.atlassian.net`)
- `RELEASE_JIRA_EMAIL` (automation account email)
- `RELEASE_JIRA_PROJECT_KEY` (for example `KAN`)
- `RELEASE_READY_STATUS` (optional, defaults to `Ready to Release`)
- `RELEASE_DONE_STATUS` (optional, defaults to `Done`)

Set repository secret:
- `RELEASE_JIRA_API_TOKEN`

## Optional Jira automation rule (event-driven)
If you want immediate assignment outside the hourly cycle, create a Jira automation rule:
1. Trigger: `Issue transitioned` to `Ready to Release`.
2. Action: send web request to GitHub workflow dispatch endpoint for `release-train-sync.yml`.
3. Use a GitHub token with workflow dispatch permission.

## Manual runs
Assign ready issues (dry-run):
- GitHub Actions -> `Release Train Sync` -> `Run workflow` with `dry_run=true`.

Run full release-train close (manual):
- GitHub Actions -> `Release Train Close` -> `Run workflow` with `release_version=vX.Y.Z`.

## Notes
- Release labels replace previous `release:*` labels to ensure one active target release per issue.
- Version progression defaults to patch increments from the latest stable git tag (`vX.Y.Z`).
- Release Train Close requires GitHub workflow token permissions to merge PRs (`contents: write`, `pull-requests: write`).
- PR discovery for merge uses Jira issue keys in PR titles.
