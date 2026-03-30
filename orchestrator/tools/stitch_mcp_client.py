"""Minimal Stitch MCP client in Python (Streamable HTTP + JSON-RPC).

Mirrors the behavior of ``@google/stitch-sdk`` / ``@modelcontextprotocol/sdk`` used by
``StitchToolClient`` against ``https://stitch.googleapis.com/mcp`` with ``X-Goog-Api-Key``.

Reference upstream: https://github.com/google-labs-code/stitch-sdk/tree/main/packages/sdk/src
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

DEFAULT_STITCH_MCP_URL = "https://stitch.googleapis.com/mcp"
JSONRPC_VERSION = "2.0"
# Keep aligned with @modelcontextprotocol/sdk bundled by @google/stitch-sdk
LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {
        LATEST_PROTOCOL_VERSION,
        "2025-06-18",
        "2025-03-26",
        "2024-11-05",
        "2024-10-07",
    }
)
CLIENT_NAME = "stitch-core-client"
CLIENT_VERSION = "0.0.3"


class StitchMcpError(RuntimeError):
    """Transport or protocol error talking to Stitch MCP."""


@dataclass(frozen=True)
class _JsonRpcError:
    code: int
    message: str
    data: Any = None


def _parse_json_rpc_error(payload: dict[str, Any]) -> _JsonRpcError | None:
    err = payload.get("error")
    if not isinstance(err, dict):
        return None
    code = err.get("code")
    message = str(err.get("message") or "rpc_error")
    return _JsonRpcError(code=int(code) if code is not None else -32603, message=message, data=err.get("data"))


def parse_stitch_tool_mcp_result(result: dict[str, Any], *, tool_name: str) -> Any:
    """Match ``StitchToolClient.parseToolResponse`` for a ``tools/call`` *result* object."""
    if bool(result.get("isError")):
        content = result.get("content") or []
        parts: list[str] = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
        error_text = "".join(parts) or "tool_error"
        lower = error_text.lower()
        if "rate limit" in lower or "429" in lower:
            code = "RATE_LIMITED"
        elif "not found" in lower or "404" in lower:
            code = "NOT_FOUND"
        elif "permission" in lower or "403" in lower:
            code = "PERMISSION_DENIED"
        else:
            code = "UNKNOWN_ERROR"
        raise StitchMcpError(f"Tool Call Failed [{tool_name}]: {error_text} ({code})")

    if result.get("structuredContent") is not None:
        return result.get("structuredContent")
    content = result.get("content") or []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text") or "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
    return result


def _iter_sse_events(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    """Parse SSE ``data:`` frames; events end on a blank line (or EOF)."""
    data_lines: list[str] = []

    def _flush() -> dict[str, Any] | None:
        if not data_lines:
            return None
        blob = "\n".join(data_lines)
        data_lines.clear()
        try:
            msg = json.loads(blob)
        except json.JSONDecodeError:
            logger.debug("stitch_mcp_sse_skip_non_json")
            return None
        return msg if isinstance(msg, dict) else None

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if line.startswith(":"):
            continue
        if not line:
            flushed = _flush()
            if flushed is not None:
                yield flushed
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
    flushed = _flush()
    if flushed is not None:
        yield flushed


class StitchMcpClient:
    """One-shot MCP session: ``connect()`` then ``call_tool`` / ``tools/list``."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_STITCH_MCP_URL,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._api_key = str(api_key or "").strip()
        if not self._api_key:
            raise StitchMcpError("Stitch API key is required")
        self._base_url = str(base_url or DEFAULT_STITCH_MCP_URL).rstrip("/")
        self._timeout = float(timeout_seconds)
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._next_id = 0

    def _alloc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._api_key,
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        if self._protocol_version:
            headers["mcp-protocol-version"] = self._protocol_version
        return headers

    def _ingest_response_session(self, resp: Any) -> None:
        sid = resp.headers.get("mcp-session-id") if hasattr(resp, "headers") else None
        if sid:
            self._session_id = str(sid).strip() or self._session_id

    def _post_raw(self, body: bytes) -> tuple[Any, bytes]:
        req = Request(
            self._base_url,
            data=body,
            method="POST",
            headers=self._build_headers(),
        )
        try:
            resp = urlopen(req, timeout=self._timeout)
        except HTTPError as exc:
            try:
                payload = exc.read()
            except OSError:
                payload = b""
            raise StitchMcpError(f"HTTP {exc.code}: {payload[:500]!r}") from exc
        except URLError as exc:
            raise StitchMcpError(f"HTTP transport failed: {exc}") from exc
        self._ingest_response_session(resp)
        data = resp.read()
        return resp, data

    def _post_stream_lines(self, body: bytes) -> Iterator[str]:
        req = Request(
            self._base_url,
            data=body,
            method="POST",
            headers=self._build_headers(),
        )
        try:
            resp = urlopen(req, timeout=self._timeout)
        except HTTPError as exc:
            try:
                payload = exc.read()
            except OSError:
                payload = b""
            raise StitchMcpError(f"HTTP {exc.code}: {payload[:500]!r}") from exc
        except URLError as exc:
            raise StitchMcpError(f"HTTP transport failed: {exc}") from exc
        self._ingest_response_session(resp)
        charset = "utf-8"
        try:
            parsed_charset = resp.headers.get_content_charset()
        except Exception:
            parsed_charset = None
        if parsed_charset:
            charset = parsed_charset
        buffer = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield line.decode(charset, errors="replace")
        if buffer:
            yield buffer.decode(charset, errors="replace")
        try:
            resp.close()
        except Exception:
            pass

    def _json_rpc_result_dict(self, *, request_id: int, payload: Any) -> dict[str, Any]:
        messages = payload if isinstance(payload, list) else [payload]
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("id") != request_id:
                continue
            err = _parse_json_rpc_error(msg)
            if err is not None:
                raise StitchMcpError(f"RPC error {err.code}: {err.message}")
            result = msg.get("result")
            if not isinstance(result, dict):
                raise StitchMcpError("RPC result is not an object")
            return result
        raise StitchMcpError("JSON-RPC response missing matching result")

    def _send_request(self, payload: dict[str, Any], *, request_id: int) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        lines_iter = self._post_stream_lines(body)
        buffered_lines: list[str] = []
        for line in lines_iter:
            if not str(line).strip():
                continue
            buffered_lines.append(line)
            break
        if not buffered_lines:
            raise StitchMcpError("Empty response from Stitch MCP")

        stripped = buffered_lines[0].lstrip()
        if stripped.startswith("event:") or stripped.startswith("data:"):

            def _all_lines() -> Iterator[str]:
                yield from buffered_lines
                for rest in lines_iter:
                    yield rest

            for msg in _iter_sse_events(_all_lines()):
                if msg.get("id") != request_id:
                    continue
                err = _parse_json_rpc_error(msg)
                if err is not None:
                    raise StitchMcpError(f"RPC error {err.code}: {err.message}")
                result = msg.get("result")
                if not isinstance(result, dict):
                    raise StitchMcpError("RPC SSE result is not an object")
                return result
            raise StitchMcpError("SSE response missing matching result")

        rest_bytes = "\n".join(buffered_lines).encode("utf-8")
        for extra in lines_iter:
            rest_bytes += b"\n" + extra.encode("utf-8")
        try:
            payload_json = json.loads(rest_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise StitchMcpError(f"Non-SSE non-JSON response: {rest_bytes[:300]!r}") from exc
        return self._json_rpc_result_dict(request_id=request_id, payload=payload_json)

    def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        note: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
        if params is not None:
            note["params"] = params
        body = json.dumps(note).encode("utf-8")
        resp, data = self._post_raw(body)
        if resp.status not in (200, 202):
            raise StitchMcpError(f"Notification failed HTTP {resp.status}: {data[:300]!r}")

    def connect(self) -> None:
        req_id = self._alloc_id()
        init_payload: dict[str, Any] = {
            "jsonrpc": JSONRPC_VERSION,
            "id": req_id,
            "method": "initialize",
            "params": {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        }
        result = self._send_request(init_payload, request_id=req_id)
        protocol_version = str(result.get("protocolVersion") or "").strip()
        if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise StitchMcpError(f"Unsupported MCP protocol version: {protocol_version!r}")
        self._protocol_version = protocol_version
        self._send_notification("notifications/initialized")

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        req_id = self._alloc_id()
        payload: dict[str, Any] = {
            "jsonrpc": JSONRPC_VERSION,
            "id": req_id,
            "method": "tools/call",
            "params": {"name": str(name or "").strip(), "arguments": dict(arguments or {})},
        }
        mcp_result = self._send_request(payload, request_id=req_id)
        return parse_stitch_tool_mcp_result(mcp_result, tool_name=name)

    def list_tools(self) -> list[dict[str, Any]]:
        req_id = self._alloc_id()
        payload: dict[str, Any] = {
            "jsonrpc": JSONRPC_VERSION,
            "id": req_id,
            "method": "tools/list",
            "params": {},
        }
        result = self._send_request(payload, request_id=req_id)
        tools = result.get("tools")
        return [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []

    def close(self) -> None:
        if not self._session_id:
            return
        try:
            req = Request(
                self._base_url,
                method="DELETE",
                headers=self._build_headers(),
            )
            urlopen(req, timeout=min(self._timeout, 60.0))
        except Exception:
            logger.debug("stitch_mcp_session_delete_failed", exc_info=True)
        finally:
            self._session_id = None

    def __enter__(self) -> StitchMcpClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
