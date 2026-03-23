2026-03-23

- When investigating config regressions, do not keep pushing environment-variable explanations after the user says the value is stored in the UI/secret manager. Verify the exact read path against the exact write path first.
- For Discord config specifically, distinguish platform-scoped secrets from tenant-scoped secrets and tenant `discord_config`. A value existing in `tenant/<tenant_id>/DISCORD_GUILD_ID` or `tenant.discord_config.guild_id` does not help if runtime code only reads `platform/DISCORD_GUILD_ID`.
