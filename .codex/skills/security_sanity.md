# Skill: Security & Privacy Sanity Check

## Goal
Prevent accidental security/privacy regressions.

## Checks
- Secrets: ensure no tokens/keys are introduced or logged.
- PII: ensure no customer data is logged or posted to Jira/Discord/PR.
- Auth: ensure no endpoint bypasses auth/roles.
- Input validation: validate untrusted inputs.
- Logging: structured logs, no sensitive fields.

## Output
- Summarize any risks found.
- If a risk requires product/security input, trigger Decision Gate and stop.
