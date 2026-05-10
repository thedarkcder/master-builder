from __future__ import annotations

from dataclasses import is_dataclass

from jinja2 import meta

from orchestrator.core.prompt_domain_models import _DOMAIN_MODELS_BY_TEMPLATE
from orchestrator.core.prompt_templates import _jinja_environment, _prompts_dir, render_prompt
from orchestrator.core.prompt_domain_models import (
    prompt_domain_model_for_template,
    registered_prompt_domain_model_templates,
)


def _template_context(template_name: str) -> dict[str, object]:
    env = _jinja_environment()
    source = (_prompts_dir() / template_name).read_text(encoding="utf-8")
    variables = meta.find_undeclared_variables(env.parse(source))
    return {
        variable: "{}"
        for variable in variables
        if variable != "domain_model"
    }


def test_registered_prompt_templates_render_injected_domain_model() -> None:
    for template_name in registered_prompt_domain_model_templates():
        template_path = _prompts_dir() / template_name
        raw_template = template_path.read_text(encoding="utf-8")
        domain_model = prompt_domain_model_for_template(template_name)

        assert domain_model
        assert "{{ domain_model" in raw_template

        rendered_prompt = render_prompt(template_name, **_template_context(template_name))
        assert str(domain_model["name"]) in rendered_prompt


def test_output_domains_are_not_hand_written_in_templates() -> None:
    prompt_root = _prompts_dir()
    contract_markers = (
        "Return strict JSON only with keys",
        "Return strict JSON with keys",
        "Return strict JSON with this shape",
        "Output contract:",
    )

    offenders: list[str] = []
    for template_path in prompt_root.rglob("*.j2"):
        relative_path = template_path.relative_to(prompt_root)
        raw_template = template_path.read_text(encoding="utf-8")
        if any(marker in raw_template for marker in contract_markers):
            offenders.append(str(relative_path))

    assert offenders == []


def test_prompt_domain_contracts_are_generated_from_typed_payload_models() -> None:
    assert _DOMAIN_MODELS_BY_TEMPLATE
    for template_name, model_type in _DOMAIN_MODELS_BY_TEMPLATE.items():
        prompt_model = prompt_domain_model_for_template(template_name)
        assert is_dataclass(model_type)
        assert not model_type.__name__.endswith("Payload")
        assert prompt_model is not None
        assert "Payload" not in str(prompt_model["name"])


def test_prompt_domain_contracts_do_not_use_parallel_generic_domain_models() -> None:
    repo_root = _prompts_dir().parents[1]
    offenders: list[str] = []
    banned_markers = ("DomainModel", "DomainField", "domain_field", "JsonOutputContract")

    for relative_path in (
        "orchestrator/core/prompt_domain_models.py",
        "orchestrator/core/prompt_model_schema.py",
        "orchestrator/core/runtime/payload_models.py",
        "orchestrator/core/workflow/runner.py",
    ):
        source = (repo_root / relative_path).read_text(encoding="utf-8")
        if any(marker in source for marker in banned_markers):
            offenders.append(relative_path)

    assert not (repo_root / "orchestrator/core/domain_spec.py").exists()
    assert not (repo_root / "orchestrator/core/decision/domain_models.py").exists()
    assert not (repo_root / "orchestrator/core/integrations/atlassian/domain_models.py").exists()
    assert not (repo_root / "orchestrator/core/issue_fanout_domain_models.py").exists()
    assert not (repo_root / "orchestrator/core/planning/domain_models.py").exists()
    assert not (repo_root / "orchestrator/core/pm/domain_models.py").exists()
    assert not (repo_root / "orchestrator/core/review/domain_models.py").exists()
    assert not (repo_root / "orchestrator/core/runtime/agent_domain_models.py").exists()
    assert offenders == []
