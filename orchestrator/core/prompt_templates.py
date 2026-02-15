from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

try:  # pragma: no cover - optional dependency
    from jinja2 import Environment, FileSystemLoader, StrictUndefined
except ModuleNotFoundError:  # pragma: no cover - exercised in fallback path tests
    Environment = None  # type: ignore[assignment]
    FileSystemLoader = None  # type: ignore[assignment]
    StrictUndefined = None  # type: ignore[assignment]

_TEMPLATE_VAR_PATTERN = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")


def _prompts_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "prompts"


@lru_cache(maxsize=1)
def _jinja_environment() -> Any | None:
    if Environment is None or FileSystemLoader is None:
        return None
    return Environment(
        loader=FileSystemLoader(str(_prompts_dir())),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _render_fallback(template_text: str, context: dict[str, Any]) -> str:
    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(context.get(key, ""))

    return _TEMPLATE_VAR_PATTERN.sub(_replace, template_text)


def render_prompt(template_name: str, **context: Any) -> str:
    env = _jinja_environment()
    if env is not None:
        template = env.get_template(template_name)
        return str(template.render(**context)).strip()

    template_path = _prompts_dir() / template_name
    template_text = template_path.read_text(encoding="utf-8")
    return _render_fallback(template_text, context).strip()
