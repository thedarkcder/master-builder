# Jira Release-Train Automation

This repo supports Jira-driven release-train automation without manually setting `Fix Version`.

## Goal
- When an issue transitions to `READY TO RELEASE`, it is automatically assigned a release label: `release:vX.Y.Z`.
- Release-train automation can merge matching PRs to `main` and transition Jira issues to `Done`.

## Workflows
- `.github/workflows/release-train-sync.yml`
  - Polls Jira every 3 minutes (and via manual dispatch).
  - Finds `READY TO RELEASE` issues and assigns `release:vX.Y.Z` automatically.
  - If no explicit version is passed, computes the next release from git tags.
- `.github/workflows/release-train-close.yml`
  - Runs hourly (and via manual dispatch).
  - Resolves target release version.
  - Finds issues tagged for the release (`release:vX.Y.Z`).
  - Waits for required checks on matching open PRs, then merges to `main`.
  - Transitions Jira issues labeled `release:vX.Y.Z` to `Done`.

## Required GitHub configuration
Set repository variable:
- `ENABLE_JIRA_RELEASE_AUTOMATION=true`

Set repository variables:
- `RELEASE_JIRA_BASE_URL` (for example `https://your-org.atlassian.net`)
- `RELEASE_JIRA_CLOUD_ID` (optional; required when using scoped Atlassian API tokens)
- `RELEASE_JIRA_EMAIL` (automation account email)
- `RELEASE_JIRA_PROJECT_KEY` (for example `KAN`)
- `RELEASE_DONE_STATUS` (optional, defaults to `Done`)

Set repository secret:
- `RELEASE_JIRA_API_TOKEN`

## Optional Jira automation rule (event-driven)
If you want immediate assignment (instead of polling), create a Jira automation rule:
1. Trigger: `Issue transitioned` to `READY TO RELEASE`.
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
