from orchestrator.api.discord import discord_channel_scope_repository as _impl

globals().update({name: getattr(_impl, name) for name in dir(_impl) if not name.startswith("__")})
