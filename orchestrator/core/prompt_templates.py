from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from orchestrator.core.prompt_domain_models import prompt_domain_model_for_template


def _prompts_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "prompts"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


_SKILLS_BY_TEMPLATE: dict[str, tuple[str, ...]] = {
    "workflow/review_system.j2": ("staff-engineer-review",),
}


@lru_cache(maxsize=1)
def _jinja_environment() -> Any:
    try:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependency
        raise RuntimeError(
            "Prompt rendering requires jinja2; install it in the runtime environment"
        ) from exc
    return Environment(
        loader=FileSystemLoader(str(_prompts_dir())),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


@lru_cache(maxsize=None)
def _load_skill_prompt_text(skill_name: str) -> str:
    normalized_name = str(skill_name or "").strip()
    if not normalized_name or "/" in normalized_name or "\\" in normalized_name:
        raise ValueError(f"Invalid prompt skill name: {skill_name!r}")
    skill_path = _repo_root() / ".codex" / "skills" / normalized_name / "SKILL.md"
    if not skill_path.is_file():
        raise RuntimeError(
            f"Required prompt skill is missing: {normalized_name} at {skill_path}"
        )
    return skill_path.read_text(encoding="utf-8").strip()


def _prompt_skills_for_template(template_name: str) -> dict[str, str]:
    skill_names = _SKILLS_BY_TEMPLATE.get(template_name, ())
    return {
        skill_name: _load_skill_prompt_text(skill_name) for skill_name in skill_names
    }


def render_prompt(template_name: str, **context: Any) -> str:
    env = _jinja_environment()
    template = env.get_template(template_name)
    render_context = dict(context)
    domain_model = prompt_domain_model_for_template(template_name)
    if domain_model is not None:
        render_context.setdefault("domain_model", domain_model)
    required_skills = _prompt_skills_for_template(template_name)
    if required_skills:
        render_context["skills"] = required_skills
    else:
        render_context.setdefault("skills", {})
    return str(template.render(**render_context)).strip()
