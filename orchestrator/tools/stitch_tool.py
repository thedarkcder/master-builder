"""Google Stitch **tool** integration for the design stage SPI.

Python port of the Stitch domain flow in ``@google/stitch-sdk`` (see upstream
`packages/sdk/src` in https://github.com/google-labs-code/stitch-sdk ): MCP
``create_project`` → ``generate_screen_from_text`` → screen preview URLs, over
Streamable HTTP JSON-RPC to ``https://stitch.googleapis.com/mcp`` with
``X-Goog-Api-Key``.

The Stitch API key is resolved from tenant secrets (secret ref: ``STITCH_API_KEY``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from orchestrator.tools.stitch_mcp_client import (
    DEFAULT_STITCH_MCP_URL,
    StitchMcpClient,
    StitchMcpError,
)

logger = logging.getLogger(__name__)

_STITCH_API_KEY_SECRET_REF = "STITCH_API_KEY"


class StitchToolError(RuntimeError):
    """Raised when Stitch tool calls fail."""


def _normalize_project_id(data: Any) -> str:
    """Match ``Project`` constructor id extraction in generated ``project.js``."""
    if isinstance(data, str):
        v = data
    elif isinstance(data, dict):
        v = data.get("name") or data.get("id") or ""
    else:
        v = ""
    v = str(v or "").strip()
    if v.startswith("projects/"):
        v = v[9:]
    return v


def _first_generated_screen(raw: Any) -> dict[str, Any]:
    """Match ``Project.generate`` screen selection in generated ``project.js``."""
    if not isinstance(raw, dict):
        return {}
    comps = raw.get("outputComponents")
    if not isinstance(comps, list) or not comps:
        return {}
    first = comps[0]
    if not isinstance(first, dict):
        return {}
    design = first.get("design")
    if not isinstance(design, dict):
        return {}
    screens = design.get("screens")
    if not isinstance(screens, list) or not screens:
        return {}
    s0 = screens[0]
    return s0 if isinstance(s0, dict) else {}


def _screen_html_url(screen: dict[str, Any]) -> str:
    hc = screen.get("htmlCode")
    if isinstance(hc, dict) and hc.get("downloadUrl"):
        return str(hc.get("downloadUrl") or "").strip()
    return ""


def _screen_image_url(screen: dict[str, Any]) -> str:
    sh = screen.get("screenshot")
    if isinstance(sh, dict) and sh.get("downloadUrl"):
        return str(sh.get("downloadUrl") or "").strip()
    return ""


def list_stitch_tools(*, api_key: str) -> list[dict[str, Any]]:
    """List Stitch MCP tools (name, description), mirroring ``StitchToolClient.listTools``."""
    try:
        with StitchMcpClient(api_key) as client:
            client.connect()
            tools = client.list_tools()
    except StitchMcpError as exc:
        raise StitchToolError(str(exc)) from exc
    out: list[dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        out.append(
            {
                "name": str(t.get("name") or ""),
                "description": str(t.get("description") or ""),
            }
        )
    return out


def call_stitch_tool(*, api_key: str, tool: str, arguments: dict[str, Any] | None = None) -> Any:
    """Invoke a single MCP tool by name, mirroring ``StitchToolClient.callTool``."""
    try:
        with StitchMcpClient(api_key) as client:
            client.connect()
            return client.call_tool(str(tool or "").strip(), dict(arguments or {}))
    except StitchMcpError as exc:
        raise StitchToolError(str(exc)) from exc


def synthesize_stitch_screen(
    *,
    api_key: str,
    prompt: str,
    project_title: str = "Orchestrator design",
    device_type: str = "DESKTOP",
    base_url: str = DEFAULT_STITCH_MCP_URL,
) -> dict[str, Any]:
    """Create a Stitch project and generate one screen; returns URLs and ids."""
    try:
        with StitchMcpClient(api_key, base_url=base_url) as client:
            client.connect()
            raw_project = client.call_tool("create_project", {"title": project_title[:120]})
            project_id = _normalize_project_id(raw_project)
            if not project_id:
                raise StitchToolError("create_project did not return a project id")
            raw_gen = client.call_tool(
                "generate_screen_from_text",
                {
                    "projectId": project_id,
                    "prompt": prompt,
                    "deviceType": device_type,
                },
            )
            screen = _first_generated_screen(raw_gen)
            screen_id = str(screen.get("id") or "").strip()
            html_url = _screen_html_url(screen)
            image_url = _screen_image_url(screen)
            if (not html_url or not image_url) and screen_id:
                name = f"projects/{project_id}/screens/{screen_id}"
                detail = client.call_tool(
                    "get_screen",
                    {
                        "projectId": project_id,
                        "screenId": screen_id,
                        "name": name,
                    },
                )
                if isinstance(detail, dict):
                    if not html_url:
                        html_url = str((detail.get("htmlCode") or {}).get("downloadUrl") or "").strip()
                    if not image_url:
                        image_url = str((detail.get("screenshot") or {}).get("downloadUrl") or "").strip()
            return {
                "provider": "stitch",
                "kind": "stitch_tool",
                "tool": "synthesize_screen",
                "project_id": project_id,
                "screen_id": screen_id,
                "html_url": html_url,
                "image_url": image_url,
            }
    except StitchMcpError as exc:
        raise StitchToolError(str(exc)) from exc


def _resolve_stitch_api_key_from_tenant_secret(
    *,
    settings: Any,
    tenant_id: str,
    project_id: str | None,
) -> str | None:
    """Resolve ``STITCH_API_KEY`` via managed tenant secrets.

    This is intentionally done inside the Stitch tool boundary to avoid
    leaking secrets into stage-planning pure logic.
    """
    secrets_encryption_key = str(getattr(settings, "secrets_encryption_key", "") or "").strip()
    if not secrets_encryption_key:
        logger.info("No secrets_encryption_key configured; skipping Stitch tool")
        return None
    if not tenant_id or not str(tenant_id).strip():
        return None

    from orchestrator.core.secret_manager import resolve_scoped_secret_ref
    from orchestrator.storage.db import create_session_factory

    session_factory = create_session_factory()
    session = session_factory()
    try:
        return resolve_scoped_secret_ref(
            session,
            secret_ref=_STITCH_API_KEY_SECRET_REF,
            tenant_id=str(tenant_id).strip(),
            project_id=str(project_id).strip() if project_id and str(project_id).strip() else None,
            encryption_key=secrets_encryption_key,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to resolve STITCH_API_KEY from tenant secrets: %s", exc)
        return None
    finally:
        session.close()


def maybe_invoke_stitch_tool_for_stage_plan(
    *,
    brief: str,
    tenant_id: str,
    project_id: str | None,
) -> dict[str, Any] | None:
    """Optional Stitch tool call on first design-stage plan; returns a ``stage_tool_outputs`` row or None."""
    from orchestrator.core.config import get_settings

    settings = get_settings()
    try:
        stitch_api_key = _resolve_stitch_api_key_from_tenant_secret(
            settings=settings,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if not stitch_api_key:
            return None

        title = f"MB {tenant_id}"[:120]
        if project_id:
            title = f"{title} {project_id}"[:120]
        result = synthesize_stitch_screen(
            api_key=stitch_api_key,
            prompt=str(brief or "").strip() or "Product design direction",
            project_title=title,
        )
        result = dict(result)
        result.setdefault("captured_at", datetime.now(timezone.utc).isoformat())
        result.setdefault("tenant_id", str(tenant_id or "").strip())
        result.setdefault("project_id", str(project_id or "").strip())
        return result
    except StitchToolError as exc:
        logger.warning("Stitch tool invocation failed: %s", exc)
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Stitch tool failed: %s", exc)
        return None
