from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request, urlopen


class DiscordApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscordTextChannel:
    channel_id: str
    name: str
    parent_id: str | None


@dataclass(frozen=True)
class DiscordVoiceChannel:
    channel_id: str
    name: str
    parent_id: str | None


@dataclass(frozen=True)
class DiscordCategoryChannel:
    channel_id: str
    name: str


class DiscordApiClient:
    def __init__(self, *, bot_token: str):
        token = bot_token.strip()
        if not token:
            raise ValueError("Discord bot token cannot be empty")
        self._bot_token = token
        self._base_url = "https://discord.com/api/v10"

    def _request_json(
        self, *, method: str, path: str, payload: dict | None = None
    ) -> dict | list:
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
            if (
                exc.code == 403
                and "Cloudflare" in error_body
                and "Error 1010" in error_body
            ):
                raise DiscordApiError(
                    "Discord API request was blocked by Cloudflare (Error 1010). "
                    "This is an egress/IP or client-fingerprint block, not a bot-token validation failure."
                ) from exc
            raise DiscordApiError(
                f"Discord API request failed ({exc.code}): {error_body}"
            ) from exc
        except URLError as exc:
            raise DiscordApiError(
                f"Discord API request failed (network): {exc}"
            ) from exc

        if not raw_body:
            return {}
        return json.loads(raw_body)

    def _request_multipart(
        self,
        *,
        method: str,
        path: str,
        payload_json: dict,
        file_field: str,
        file_name: str,
        file_bytes: bytes,
        file_content_type: str = "application/octet-stream",
    ) -> dict | list:
        boundary = f"----master-builder-discord-boundary-{uuid4().hex}"
        normalized_content_type = (
            file_content_type.strip() or "application/octet-stream"
        )
        encoded_payload = json.dumps(payload_json, separators=(",", ":")).encode(
            "utf-8"
        )
        body = b"".join(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                b'Content-Disposition: form-data; name="payload_json"\r\n',
                b"Content-Type: application/json\r\n\r\n",
                encoded_payload,
                b"\r\n",
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
                    f"Content-Type: {normalized_content_type}\r\n\r\n"
                ).encode("utf-8"),
                file_bytes,
                b"\r\n",
                f"--{boundary}--\r\n".encode("utf-8"),
            ]
        )
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bot {self._bot_token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "MasterBuilderDiscordClient/1.0 (+https://github.com/thedarkcder/master-builder)",
        }
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
            if (
                exc.code == 403
                and "Cloudflare" in error_body
                and "Error 1010" in error_body
            ):
                raise DiscordApiError(
                    "Discord API request was blocked by Cloudflare (Error 1010). "
                    "This is an egress/IP or client-fingerprint block, not a bot-token validation failure."
                ) from exc
            raise DiscordApiError(
                f"Discord API request failed ({exc.code}): {error_body}"
            ) from exc
        except URLError as exc:
            raise DiscordApiError(
                f"Discord API request failed (network): {exc}"
            ) from exc

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
            normalized_parent = (
                parent_id if isinstance(parent_id, str) and parent_id.strip() else None
            )
            channels.append(
                DiscordTextChannel(
                    channel_id=channel_id, name=name, parent_id=normalized_parent
                )
            )
        return channels

    def list_voice_channels(self, *, guild_id: str) -> list[DiscordVoiceChannel]:
        payload = self._request_json(method="GET", path=f"/guilds/{guild_id}/channels")
        if not isinstance(payload, list):
            raise DiscordApiError("Discord list channels response was not a list")

        channels: list[DiscordVoiceChannel] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            if item.get("type") != 2:
                continue
            channel_id = item.get("id")
            name = item.get("name")
            parent_id = item.get("parent_id")
            if not isinstance(channel_id, str) or not channel_id:
                continue
            if not isinstance(name, str) or not name:
                continue
            normalized_parent = (
                parent_id if isinstance(parent_id, str) and parent_id.strip() else None
            )
            channels.append(
                DiscordVoiceChannel(
                    channel_id=channel_id, name=name, parent_id=normalized_parent
                )
            )
        return channels

    def list_channel_categories(self, *, guild_id: str) -> list[DiscordCategoryChannel]:
        payload = self._request_json(method="GET", path=f"/guilds/{guild_id}/channels")
        if not isinstance(payload, list):
            raise DiscordApiError("Discord list channels response was not a list")

        categories: list[DiscordCategoryChannel] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            if item.get("type") != 4:
                continue
            channel_id = item.get("id")
            name = item.get("name")
            if not isinstance(channel_id, str) or not channel_id:
                continue
            if not isinstance(name, str) or not name:
                continue
            categories.append(DiscordCategoryChannel(channel_id=channel_id, name=name))
        return categories

    def create_text_channel(
        self, *, guild_id: str, name: str, parent_id: str | None = None
    ) -> DiscordTextChannel:
        payload: dict = {"name": name, "type": 0}
        if parent_id:
            payload["parent_id"] = parent_id
        data = self._request_json(
            method="POST", path=f"/guilds/{guild_id}/channels", payload=payload
        )
        if not isinstance(data, dict):
            raise DiscordApiError("Discord create channel response was not an object")
        channel_id = data.get("id")
        created_name = data.get("name")
        created_parent = data.get("parent_id")
        if not isinstance(channel_id, str) or not channel_id:
            raise DiscordApiError("Discord create channel response missing id")
        if not isinstance(created_name, str) or not created_name:
            raise DiscordApiError("Discord create channel response missing name")
        normalized_parent = (
            created_parent
            if isinstance(created_parent, str) and created_parent.strip()
            else None
        )
        return DiscordTextChannel(
            channel_id=channel_id, name=created_name, parent_id=normalized_parent
        )

    def create_voice_channel(
        self, *, guild_id: str, name: str, parent_id: str | None = None
    ) -> DiscordVoiceChannel:
        payload: dict = {"name": name, "type": 2}
        if parent_id:
            payload["parent_id"] = parent_id
        data = self._request_json(
            method="POST", path=f"/guilds/{guild_id}/channels", payload=payload
        )
        if not isinstance(data, dict):
            raise DiscordApiError("Discord create channel response was not an object")
        channel_id = data.get("id")
        created_name = data.get("name")
        created_parent = data.get("parent_id")
        if not isinstance(channel_id, str) or not channel_id:
            raise DiscordApiError("Discord create channel response missing id")
        if not isinstance(created_name, str) or not created_name:
            raise DiscordApiError("Discord create channel response missing name")
        normalized_parent = (
            created_parent
            if isinstance(created_parent, str) and created_parent.strip()
            else None
        )
        return DiscordVoiceChannel(
            channel_id=channel_id, name=created_name, parent_id=normalized_parent
        )

    def post_message(
        self,
        *,
        channel_id: str,
        content: str,
        components: list[dict] | None = None,
        flags: int | None = None,
    ) -> dict:
        normalized_channel_id = channel_id.strip()
        normalized_content = content.strip()
        if not normalized_channel_id:
            raise ValueError("Discord channel ID cannot be empty")
        if not normalized_content:
            raise ValueError("Discord message content cannot be empty")
        payload: dict[str, object] = {"content": normalized_content}
        if components:
            payload["components"] = components
        if flags is not None:
            payload["flags"] = int(flags)
        data = self._request_json(
            method="POST",
            path=f"/channels/{normalized_channel_id}/messages",
            payload=payload,
        )
        if not isinstance(data, dict):
            raise DiscordApiError("Discord create message response was not an object")
        return data

    def post_message_with_attachment(
        self,
        *,
        channel_id: str,
        content: str,
        filename: str,
        file_bytes: bytes,
        content_type: str = "application/octet-stream",
        components: list[dict] | None = None,
        flags: int | None = None,
    ) -> dict:
        normalized_channel_id = channel_id.strip()
        normalized_content = content.strip()
        normalized_filename = filename.strip()
        if not normalized_channel_id:
            raise ValueError("Discord channel ID cannot be empty")
        if not normalized_filename:
            raise ValueError("Discord attachment filename cannot be empty")
        if not file_bytes:
            raise ValueError("Discord attachment payload cannot be empty")
        payload: dict[str, object] = {}
        if normalized_content:
            payload["content"] = normalized_content
        if components:
            payload["components"] = components
        if flags is not None:
            payload["flags"] = int(flags)
        data = self._request_multipart(
            method="POST",
            path=f"/channels/{normalized_channel_id}/messages",
            payload_json=payload,
            file_field="files[0]",
            file_name=normalized_filename,
            file_bytes=file_bytes,
            file_content_type=content_type,
        )
        if not isinstance(data, dict):
            raise DiscordApiError("Discord create message response was not an object")
        return data

    def get_channel(self, *, channel_id: str) -> dict:
        normalized_channel_id = channel_id.strip()
        if not normalized_channel_id:
            raise ValueError("Discord channel ID cannot be empty")
        data = self._request_json(
            method="GET",
            path=f"/channels/{normalized_channel_id}",
        )
        if not isinstance(data, dict):
            raise DiscordApiError("Discord get channel response was not an object")
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

    def create_invite(
        self,
        *,
        channel_id: str,
        max_age: int | None = None,
        max_uses: int | None = None,
        unique: bool = True,
    ) -> dict:
        normalized_channel_id = channel_id.strip()
        if not normalized_channel_id:
            raise ValueError("Discord channel ID cannot be empty")
        payload: dict[str, object] = {"unique": unique}
        if max_age is not None:
            payload["max_age"] = int(max_age)
        if max_uses is not None:
            payload["max_uses"] = int(max_uses)
        data = self._request_json(
            method="POST",
            path=f"/channels/{normalized_channel_id}/invites",
            payload=payload,
        )
        if not isinstance(data, dict):
            raise DiscordApiError("Discord create invite response was not an object")
        return data

    def add_guild_member(
        self,
        *,
        guild_id: str,
        user_id: str,
        user_access_token: str,
    ) -> dict:
        normalized_guild_id = guild_id.strip()
        normalized_user_id = user_id.strip()
        normalized_access_token = user_access_token.strip()
        if not normalized_guild_id:
            raise ValueError("Discord guild ID cannot be empty")
        if not normalized_user_id:
            raise ValueError("Discord user ID cannot be empty")
        if not normalized_access_token:
            raise ValueError("Discord user access token cannot be empty")
        data = self._request_json(
            method="PUT",
            path=f"/guilds/{normalized_guild_id}/members/{normalized_user_id}",
            payload={"access_token": normalized_access_token},
        )
        if not isinstance(data, dict):
            raise DiscordApiError("Discord add guild member response was not an object")
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
        return self.create_text_channel(
            guild_id=guild_id, name=name, parent_id=parent_id
        )

    def ensure_voice_channel(
        self,
        *,
        guild_id: str,
        name: str,
        parent_id: str | None = None,
    ) -> DiscordVoiceChannel:
        for channel in self.list_voice_channels(guild_id=guild_id):
            same_parent = channel.parent_id == parent_id
            if channel.name == name and same_parent:
                return channel
        return self.create_voice_channel(
            guild_id=guild_id, name=name, parent_id=parent_id
        )

    def get_application_id(self) -> str:
        payload = self._request_json(method="GET", path="/oauth2/applications/@me")
        if not isinstance(payload, dict):
            raise DiscordApiError(
                "Discord application metadata response was not an object"
            )
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
