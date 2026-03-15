from __future__ import annotations

import re
import socket

_WORKSPACE_SEGMENT_PATTERN = re.compile(r"[^a-z0-9._-]+")


def _normalize_segment(value: object) -> str:
    raw = str(value or "").strip().lower()
    normalized = _WORKSPACE_SEGMENT_PATTERN.sub("-", raw).strip("-.")
    return normalized[:64] if normalized else "unknown"


def normalize_workspace_key(value: object) -> str:
    normalized = _WORKSPACE_SEGMENT_PATTERN.sub("-", str(value or "").strip().lower()).strip("-.")
    if not normalized:
        raise ValueError("workspace key must not be empty")
    return normalized[:128]


def resolve_worker_workspace_key(*, settings) -> str:  # noqa: ANN001
    override = str(getattr(settings, "worker_workspace_key", "") or "").strip()
    if override:
        return normalize_workspace_key(override)
    agent_id = _normalize_segment(getattr(settings, "agent_id", "worker"))
    hostname = _normalize_segment(socket.gethostname())
    return normalize_workspace_key(f"{agent_id}--{hostname}")

