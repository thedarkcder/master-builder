from unittest.mock import MagicMock

from orchestrator.core.knowledge_base import build_knowledge_prompt_context


def test_build_knowledge_prompt_context_requires_project_scope() -> None:
    session = MagicMock()

    context = build_knowledge_prompt_context(
        session=session,
        tenant_id="tenant-1",
        project_id=None,
        query="How does auth rollout work?",
    )

    assert context.text == ""
    assert context.citations == []
    session.execute.assert_not_called()


def test_build_knowledge_prompt_context_requires_tenant_scope() -> None:
    session = MagicMock()

    context = build_knowledge_prompt_context(
        session=session,
        tenant_id="",
        project_id="project-1",
        query="How does auth rollout work?",
    )

    assert context.text == ""
    assert context.citations == []
    session.execute.assert_not_called()
