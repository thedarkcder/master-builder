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

    def post_message(self, *, channel_id: str, content: str, components: list[dict] | None = None) -> dict:
        normalized_channel_id = channel_id.strip()
        normalized_content = content.strip()
        if not normalized_channel_id:
            raise ValueError("Discord channel ID cannot be empty")
        if not normalized_content:
            raise ValueError("Discord message content cannot be empty")
        payload: dict[str, object] = {"content": normalized_content}
        if components:
            payload["components"] = components
        data = self._request_json(
            method="POST",
            path=f"/channels/{normalized_channel_id}/messages",
            payload=payload,
        )
        if not isinstance(data, dict):
            raise DiscordApiError("Discord create message response was not an object")
        return data

    def get_message(self, *, channel_id: str, message_id: str) -> dict:
        normalized_channel_id = channel_id.strip()
        normalized_message_id = message_id.strip()
        if not normalized_channel_id:
            raise ValueError("Discord channel ID cannot be empty")
        if not normalized_message_id:
            raise ValueError("Discord message ID cannot be empty")
        data = self._request_json(
            method="GET",
            path=f"/channels/{normalized_channel_id}/messages/{normalized_message_id}",
        )
        if not isinstance(data, dict):
            raise DiscordApiError("Discord get message response was not an object")
        return data

    def create_thread_from_message(
        self,
        *,
        channel_id: str,
        message_id: str,
        name: str,
        auto_archive_duration: int = 1440,
    ) -> str:
        normalized_channel_id = channel_id.strip()
        normalized_message_id = message_id.strip()
        normalized_name = name.strip()
        if not normalized_channel_id:
            raise ValueError("Discord channel ID cannot be empty")
        if not normalized_message_id:
            raise ValueError("Discord message ID cannot be empty")
        if not normalized_name:
            raise ValueError("Discord thread name cannot be empty")
        payload = {
            "name": normalized_name[:100],
            "auto_archive_duration": int(auto_archive_duration),
        }
        data = self._request_json(
            method="POST",
            path=f"/channels/{normalized_channel_id}/messages/{normalized_message_id}/threads",
            payload=payload,
        )
        if not isinstance(data, dict):
            raise DiscordApiError("Discord create thread response was not an object")
        thread_id = data.get("id")
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise DiscordApiError("Discord create thread response missing id")
        return thread_id.strip()

    def ensure_thread_for_message(
        self,
        *,
        channel_id: str,
        message_id: str,
        thread_name: str,
    ) -> str:
        message = self.get_message(channel_id=channel_id, message_id=message_id)
        thread = message.get("thread")
        if isinstance(thread, dict):
            thread_id = thread.get("id")
            if isinstance(thread_id, str) and thread_id.strip():
                return thread_id.strip()
        return self.create_thread_from_message(
            channel_id=channel_id,
            message_id=message_id,
            name=thread_name,
        )

    def create_dm_channel(self, *, user_id: str) -> str:
        normalized_user_id = user_id.strip()
        if not normalized_user_id:
            raise ValueError("Discord user ID cannot be empty")
        data = self._request_json(
            method="POST",
            path="/users/@me/channels",
            payload={"recipient_id": normalized_user_id},
        )
        if not isinstance(data, dict):
            raise DiscordApiError("Discord DM channel response was not an object")
        channel_id = data.get("id")
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise DiscordApiError("Discord DM channel response missing id")
        return channel_id.strip()

    def send_direct_message(self, *, user_id: str, content: str) -> dict:
        channel_id = self.create_dm_channel(user_id=user_id)
        return self.post_message(channel_id=channel_id, content=content)

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

    def get_application_id(self) -> str:
        payload = self._request_json(method="GET", path="/oauth2/applications/@me")
        if not isinstance(payload, dict):
            raise DiscordApiError("Discord application metadata response was not an object")
        app_id = payload.get("id")
        if not isinstance(app_id, str) or not app_id.strip():
            raise DiscordApiError("Discord application metadata response missing id")
        return app_id.strip()

    def overwrite_guild_commands(
        self,
        *,
        application_id: str,
        guild_id: str,
        commands: list[dict],
    ) -> list[dict]:
        normalized_app_id = application_id.strip()
        normalized_guild_id = guild_id.strip()
        if not normalized_app_id:
            raise ValueError("Discord application ID cannot be empty")
        if not normalized_guild_id:
            raise ValueError("Discord guild ID cannot be empty")
        payload = self._request_json(
            method="PUT",
            path=f"/applications/{normalized_app_id}/guilds/{normalized_guild_id}/commands",
            payload=commands,
        )
        if not isinstance(payload, list):
            raise DiscordApiError("Discord overwrite commands response was not a list")
        return [item for item in payload if isinstance(item, dict)]
