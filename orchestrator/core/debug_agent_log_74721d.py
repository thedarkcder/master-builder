"""Session 74721d debug NDJSON logger (orchestrator). Override path with ORCHESTRATOR_DEBUG_AGENT_LOG_PATH."""
from __future__ import annotations

import json
import os
import time
from typing import Any

_DEFAULT_PATH = "/Users/aaronbedward/Documents/projects/master-builder/.cursor/debug-74721d.log"
_SESSION_ID = "74721d"


def agent_debug_log_74721d(*, hypothesis_id: str, location: str, message: str, data: dict[str, Any]) -> None:
    path = os.environ.get("ORCHESTRATOR_DEBUG_AGENT_LOG_PATH", _DEFAULT_PATH)
    payload = {
        "sessionId": _SESSION_ID,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass
