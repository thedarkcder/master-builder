from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from orchestrator.core.prompt_domain_models import prompt_domain_model_for_template


def _prompts_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "prompts"


@lru_cache(maxsize=1)
def _jinja_environment() -> Any:
    try:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependency
        raise RuntimeError("Prompt rendering requires jinja2; install it in the runtime environment") from exc
    return Environment(
        loader=FileSystemLoader(str(_prompts_dir())),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_prompt(template_name: str, **context: Any) -> str:
    env = _jinja_environment()
    template = env.get_template(template_name)
    render_context = dict(context)
    domain_model = prompt_domain_model_for_template(template_name)
    if domain_model is not None:
        render_context.setdefault("domain_model", domain_model)
    return str(template.render(**render_context)).strip()
