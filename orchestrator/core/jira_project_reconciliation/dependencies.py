from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JiraProjectReconciliationHandlerDeps:
    gateway_factory: Any | None = None
