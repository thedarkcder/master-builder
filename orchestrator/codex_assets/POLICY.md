# Policy (Hard Rules)

These rules apply to both agents and humans. Violations should block the PR.

## Absolute never-do
- Never add or expose secrets (API keys, tokens, credentials) in code, config, logs, comments, PRs, or Jira.
- Never include customer PII in logs, PR comments, Jira comments, or Discord messages.
- Never run destructive shell commands (`rm -rf`, `sudo`, `chmod 777`, `curl | sh`, etc.).
- Never synchronize using sleeps/timers:
  - `Thread.sleep`, `time.sleep`, `setTimeout` for “waiting”
- Never auto-merge PRs.

## Required behaviors
- If ticket is not “Good To Do”, trigger Decision Gate and STOP.
- If design choice affects NFRs (MVP vs scale-ready), trigger Decision Gate and STOP.
- Keep diffs minimal; do not reformat unrelated files.
- Update/add tests when behavior changes.
- Provide “How to test” in PR and Jira.
- No placeholders or TODO stubs in production paths.
  - If partial work unavoidable: create Backlog follow-up issue and stop.

## Allowed Jira actions (default)
- Add comments
- Add/remove labels (if configured)
- Create follow-up issues in Backlog
- Do NOT move issues into To Do or change statuses unless explicitly enabled for tenant.

## Allowed GitHub actions
- Create branches, commits, PRs
- Comment on PRs
- Request review (optional)
- Do NOT merge
