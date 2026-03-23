2026-03-23

- When investigating config regressions, do not keep pushing environment-variable explanations after the user says the value is stored in the UI/secret manager. Verify the exact read path against the exact write path first.
- For Discord config specifically, distinguish platform-scoped secrets from tenant-scoped secrets and tenant `discord_config`. A value existing in `tenant/<tenant_id>/DISCORD_GUILD_ID` or `tenant.discord_config.guild_id` does not help if runtime code only reads `platform/DISCORD_GUILD_ID`.
- Decision Gate lifecycle rule: once a gate has been answered and cleared, it must never reopen for that issue. Do not reintroduce fingerprint-based reopening logic just because Jira summary/description/labels changed.
- Do not let worker execution enforce Decision Gate through a separate evaluator. Worker gate enforcement must consume the same canonical clarification service and persisted closure state as Discord and Jira.
- Do not thread route-specific booleans like `require_ask_confirmation` or `allow_plain_ask` through transport wrappers as business semantics. Command interpretation policy must be resolved centrally from ingress type so transports cannot drift.
