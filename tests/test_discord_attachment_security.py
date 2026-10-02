from __future__ import annotations

import inspect
import io
from email.message import Message
from urllib.response import addinfourl

import pytest

from orchestrator.api.discord.bug.attachments import download_discord_attachment


def _response(
    url: str,
    *,
    status: int = 200,
    body: bytes = b"test data",
    location: str | None = None,
):
    headers = Message()
    headers["Content-Type"] = "image/png"
    if location is not None:
        headers["Location"] = location
    response = addinfourl(io.BytesIO(body), headers, url, status)
    response.msg = "Test response"
    return response


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.discordapp.com/attachments/test.png",
        "https://attacker.example/file",
        "http://127.0.0.1/admin",
        "file:///etc/passwd",
        "https://evilcdn.discordapp.com/file",
        "https://cdn.discordapp.com.attacker.example/file",
        "https://user:password@cdn.discordapp.com/file",
        "https://cdn.discordapp.com:8443/file",
    ],
)
def test_attachment_url_is_rejected_before_any_network_request(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    requests = []
    monkeypatch.setattr("urllib.request._opener", None)

    def open_https(self, request):
        requests.append(request)
        return _response(request.full_url)

    monkeypatch.setattr("urllib.request.HTTPSHandler.https_open", open_https)
    monkeypatch.setattr("urllib.request.HTTPHandler.http_open", open_https)
    with pytest.raises(ValueError, match="Discord attachment"):
        download_discord_attachment(url=url)
    assert requests == []


def test_attachment_downloader_exposes_no_bot_credential_argument() -> None:
    assert "bot_token" not in inspect.signature(download_discord_attachment).parameters


def test_attachment_redirect_cannot_leave_approved_https_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []
    monkeypatch.setattr("urllib.request._opener", None)

    def open_https(self, request):
        requests.append(request)
        if len(requests) == 1:
            return _response(
                request.full_url, status=302, location="https://attacker.example/file"
            )
        return _response(request.full_url)

    monkeypatch.setattr("urllib.request.HTTPSHandler.https_open", open_https)
    with pytest.raises(ValueError, match="Discord attachment"):
        download_discord_attachment(
            url="https://cdn.discordapp.com/attachments/test.png"
        )
    assert len(requests) == 1
    assert requests[0].get_header("Authorization") is None


def test_large_attachment_response_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("urllib.request._opener", None)

    def open_https(self, request):
        return _response(request.full_url, body=b"x" * 25_000_001)

    monkeypatch.setattr("urllib.request.HTTPSHandler.https_open", open_https)
    with pytest.raises(ValueError, match="size limit"):
        download_discord_attachment(
            url="https://media.discordapp.net/attachments/test.png"
        )
