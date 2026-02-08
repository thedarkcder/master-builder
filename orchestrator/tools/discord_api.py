from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class DiscordApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscordTextChannel:
    channel_id: str
    name: str
    parent_id: str | None


class DiscordApiClient:
    def __init__(self, *, bot_token: str):
        token = bot_token.strip()
        if not token:
            raise ValueError("Discord bot token cannot be empty")
        self._bot_token = token
        self._base_url = "https://discord.com/api/v10"

    def _request_json(self, *, method: str, path: str, payload: dict | None = None) -> dict | list:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bot {self._bot_token}",
            "User-Agent": "MasterBuilderDiscordClient/1.0 (+https://github.com/thedarkcder/master-builder)",
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(
            url=f"{self._base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw_body = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8")
            if exc.code == 403 and "Cloudflare" in error_body and "Error 1010" in error_body:
                raise DiscordApiError(
                    "Discord API request was blocked by Cloudflare (Error 1010). "
                    "This is an egress/IP or client-fingerprint block, not a bot-token validation failure."
                ) from exc
            raise DiscordApiError(f"Discord API request failed ({exc.code}): {error_body}") from exc

        if not raw_body:
            return {}
        return json.loads(raw_body)

    def list_text_channels(self, *, guild_id: str) -> list[DiscordTextChannel]:
        payload = self._request_json(method="GET", path=f"/guilds/{guild_id}/channels")
        if not isinstance(payload, list):
            raise DiscordApiError("Discord list channels response was not a list")

        channels: list[DiscordTextChannel] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            if item.get("type") != 0:
                continue
            channel_id = item.get("id")
            name = item.get("name")
            parent_id = item.get("parent_id")
            if not isinstance(channel_id, str) or not channel_id:
                continue
            if not isinstance(name, str) or not name:
                continue
            normalized_parent = parent_id if isinstance(parent_id, str) and parent_id.strip() else None
            channels.append(DiscordTextChannel(channel_id=channel_id, name=name, parent_id=normalized_parent))
        return channels

    def create_text_channel(self, *, guild_id: str, name: str, parent_id: str | None = None) -> DiscordTextChannel:
        payload: dict = {"name": name, "type": 0}
        if parent_id:
            payload["parent_id"] = parent_id
        data = self._request_json(method="POST", path=f"/guilds/{guild_id}/channels", payload=payload)
        if not isinstance(data, dict):
            raise DiscordApiError("Discord create channel response was not an object")
        channel_id = data.get("id")
        created_name = data.get("name")
        created_parent = data.get("parent_id")
        if not isinstance(channel_id, str) or not channel_id:
            raise DiscordApiError("Discord create channel response missing id")
        if not isinstance(created_name, str) or not created_name:
            raise DiscordApiError("Discord create channel response missing name")
        normalized_parent = created_parent if isinstance(created_parent, str) and created_parent.strip() else None
        return DiscordTextChannel(channel_id=channel_id, name=created_name, parent_id=normalized_parent)

    def post_message(self, *, channel_id: str, content: str) -> dict:
        normalized_channel_id = channel_id.strip()
        normalized_content = content.strip()
        if not normalized_channel_id:
            raise ValueError("Discord channel ID cannot be empty")
        if not normalized_content:
            raise ValueError("Discord message content cannot be empty")
        data = self._request_json(
            method="POST",
            path=f"/channels/{normalized_channel_id}/messages",
            payload={"content": normalized_content},
        )
        if not isinstance(data, dict):
            raise DiscordApiError("Discord create message response was not an object")
        return data

    def ensure_text_channel(
        self,
        *,
        guild_id: str,
        name: str,
        parent_id: str | None = None,
    ) -> DiscordTextChannel:
        for channel in self.list_text_channels(guild_id=guild_id):
            same_parent = channel.parent_id == parent_id
            if channel.name == name and same_parent:
                return channel
        return self.create_text_channel(guild_id=guild_id, name=name, parent_id=parent_id)
