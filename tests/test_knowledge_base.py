from __future__ import annotations

from datetime import datetime, timezone
import os
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.knowledge.base import (
    _build_knowledge_text_embedding_model,
    _embed_texts,
    _knowledge_text_embedding_model,
    KnowledgeEmbeddingAccessMode,
    build_knowledge_prompt_context,
    create_knowledge_asset,
    sync_project_knowledge_from_jira,
)
from orchestrator.core.knowledge import base as knowledge_base_module
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import KnowledgeAsset, KnowledgeFact, Project, Tenant


def test_build_knowledge_text_embedding_model_respects_cache_dir_and_offline_env() -> None:
    captured: dict[str, object] = {}

    class _FakeTextEmbedding:
        def __init__(self, *, model_name: str, **kwargs) -> None:  # noqa: ANN003
            captured["model_name"] = model_name
            captured["kwargs"] = kwargs

    with (
        patch.dict(os.environ, {"HF_HOME": "/tmp/hf-cache", "HF_HUB_OFFLINE": "1"}, clear=False),
        patch.dict(sys.modules, {"fastembed": SimpleNamespace(TextEmbedding=_FakeTextEmbedding)}),
    ):
        _knowledge_text_embedding_model.cache_clear()
        try:
            _build_knowledge_text_embedding_model()
        finally:
            _knowledge_text_embedding_model.cache_clear()

    assert captured["model_name"] == "BAAI/bge-small-en-v1.5"
    assert captured["kwargs"] == {"cache_dir": "/tmp/hf-cache", "local_files_only": True}


def test_embed_texts_uses_local_cache_for_runtime_embedding_access() -> None:
    fake_model = SimpleNamespace(embed=lambda texts: [[0.1] for _ in texts])
    previous_unavailable_until = knowledge_base_module._embedding_model_unavailable_until_epoch

    try:
        knowledge_base_module._embedding_model_unavailable_until_epoch = 0.0
        with patch(
            "orchestrator.core.knowledge.base._knowledge_text_embedding_model",
            return_value=fake_model,
        ) as model_mock:
            vectors = _embed_texts(
                ["bundle id"],
                embedding_access_mode=KnowledgeEmbeddingAccessMode.BEST_EFFORT,
            )
    finally:
        knowledge_base_module._embedding_model_unavailable_until_epoch = previous_unavailable_until

    model_mock.assert_called_once_with(True)
    assert vectors == [[0.1]]


def test_embed_texts_suppresses_repeated_embedding_bootstrap_failures() -> None:
    previous_unavailable_until = knowledge_base_module._embedding_model_unavailable_until_epoch
    try:
        knowledge_base_module._embedding_model_unavailable_until_epoch = 0.0
        with patch(
            "orchestrator.core.knowledge.base._knowledge_text_embedding_model",
            side_effect=RuntimeError("embedding model unavailable"),
        ) as model_mock:
            first = _embed_texts(
                ["query-a"],
                embedding_access_mode=KnowledgeEmbeddingAccessMode.LOCAL_ONLY,
            )
            second = _embed_texts(
                ["query-b"],
                embedding_access_mode=KnowledgeEmbeddingAccessMode.LOCAL_ONLY,
            )

        assert first == [None]
        assert second == [None]
        model_mock.assert_called_once_with(True)
    finally:
        knowledge_base_module._embedding_model_unavailable_until_epoch = previous_unavailable_until


def test_build_knowledge_prompt_context_requires_project_scope() -> None:
    from unittest.mock import MagicMock

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
    from unittest.mock import MagicMock

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


def test_build_knowledge_prompt_context_returns_project_scoped_match() -> None:
    with TemporaryDirectory() as temp_dir:
        database_url = f"sqlite:///{temp_dir}/knowledge_test.db"
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        session_factory = create_session_factory(database_url)
        with session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-1",
                    name="Tenant",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config={},
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.add(
                Project(
                    project_id="project-1",
                    tenant_id="tenant-1",
                    name="Project",
                    github_repository="example/repo",
                    jira_project_key="MAB",
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()
            create_knowledge_asset(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                source_type="jira_comment",
                title="GP-122 comment",
                mime_type="text/plain",
                source_ref="comment:GP-122:1",
                text_content="Production Bundle ID is com.route25.girlpower and staging bundle ID is com.route25.girlpower.stage",
            )

            context = build_knowledge_prompt_context(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                query="What is the production bundle id for GirlPower Apple sign in?",
            )

        assert "Relevant facts:" in context.text
        assert "com.route25.girlpower" in context.text
        assert context.citations
        assert context.citations[0]["source_type"] == "jira_comment"
        assert context.citations[0]["layer"] == "knowledge_fact"


def test_create_knowledge_asset_extracts_source_agnostic_facts() -> None:
    with TemporaryDirectory() as temp_dir:
        database_url = f"sqlite:///{temp_dir}/knowledge_fact_test.db"
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        session_factory = create_session_factory(database_url)
        with session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-1",
                    name="Tenant",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config={},
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.add(
                Project(
                    project_id="project-1",
                    tenant_id="tenant-1",
                    name="Project",
                    github_repository="example/repo",
                    jira_project_key="MAB",
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

            create_knowledge_asset(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                source_type="web_page",
                title="Auth rollout note",
                mime_type="text/plain",
                source_ref="https://example.test/auth",
                text_content=(
                    "Production Bundle ID: com.route25.girlpower\n"
                    "Decision owner: Platform Identity & Security owner\n"
                    "Rollback plan: Disable the gate and retry login with the previous session policy."
                ),
            )

            facts = session.query(KnowledgeFact).order_by(KnowledgeFact.fact_type, KnowledgeFact.fact_key).all()

        assert {fact.fact_type for fact in facts} >= {"configuration", "decision_slot", "ownership", "rollout_constraint"}
        assert any(fact.fact_key == "decision_owner" and "Platform Identity" in fact.fact_value for fact in facts)
        assert any(fact.fact_key == "production_bundle_id" for fact in facts)
        assert all(fact.approval_state == "approved" for fact in facts)


def test_create_knowledge_asset_keeps_long_generic_labels_with_safe_slot_name() -> None:
    with TemporaryDirectory() as temp_dir:
        database_url = f"sqlite:///{temp_dir}/knowledge_fact_long_label.db"
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        session_factory = create_session_factory(database_url)
        with session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-1",
                    name="Tenant",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config={},
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.add(
                Project(
                    project_id="project-1",
                    tenant_id="tenant-1",
                    name="Project",
                    github_repository="example/repo",
                    jira_project_key="MAB",
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

            create_knowledge_asset(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                source_type="jira_issue",
                title="Long fact label",
                mime_type="text/plain",
                source_ref="jira:GP-122",
                text_content=(
                    "Implemented GP-122 on branch run-gp-122-ed1554de-484b-46ed-831f-781376230734 "
                    "and opened PR https://github.com/example/repo/pull/7.: Success."
                ),
            )

            facts = session.query(KnowledgeFact).filter(KnowledgeFact.fact_type == "reference_fact").all()

        assert facts
        assert any(fact.slot_name == "reference_fact" for fact in facts)
        assert all(len(fact.slot_name) <= 128 for fact in facts)
        assert any("implemented_gp_122_on_branch_run_gp_122" in fact.fact_key for fact in facts)


def test_sync_project_knowledge_from_jira_upserts_comments_and_attachments() -> None:
    with TemporaryDirectory() as temp_dir:
        database_url = f"sqlite:///{temp_dir}/knowledge_sync.db"
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        session_factory = create_session_factory(database_url)

        with session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-1",
                    name="Tenant",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config={},
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.add(
                Project(
                    project_id="project-1",
                    tenant_id="tenant-1",
                    name="Project",
                    github_repository="example/repo",
                    jira_project_key="GP",
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

            class _JiraClient:
                def __init__(self) -> None:
                    self._attachment_body = b"Bundle ID: com.route25.girlpower\nService ID: com.route25.girlpower.auth"

                def search_issues_by_jql_page(self, **_kwargs):
                    return SimpleNamespace(issues=[SimpleNamespace(key="GP-122")], next_page_token=None)

                def get_issue_detail(self, **_kwargs):
                    return SimpleNamespace(
                        key="GP-122",
                        summary="Auth gate rollout",
                        status="Blocked",
                        description="Need Apple Sign In config confirmed.",
                        labels=["agent:blocked"],
                    )

                def list_issue_comments(self, **_kwargs):
                    return [
                        SimpleNamespace(
                            comment_id="2001",
                            body="Production Bundle ID: com.route25.girlpower",
                            author_display_name="Alice",
                            updated_at=datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc),
                        )
                    ]

                def list_issue_attachments(self, **_kwargs):
                    return [
                        SimpleNamespace(
                            attachment_id="3001",
                            filename="apple-signin.txt",
                            content_url="https://jira.test/attachment/3001",
                            mime_type="text/plain",
                            size_bytes=len(self._attachment_body),
                            created_at=datetime(2026, 3, 10, 12, 30, tzinfo=timezone.utc),
                        ),
                        SimpleNamespace(
                            attachment_id="3002",
                            filename="binary.zip",
                            content_url="https://jira.test/attachment/3002",
                            mime_type="application/zip",
                            size_bytes=100,
                            created_at=datetime(2026, 3, 10, 12, 31, tzinfo=timezone.utc),
                        ),
                    ]

                def download_attachment(self, **kwargs):  # noqa: ANN003
                    if kwargs["content_url"].endswith("3001"):
                        return self._attachment_body
                    raise AssertionError("unexpected attachment download")

            result = sync_project_knowledge_from_jira(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                project_key="GP",
                jira_client=_JiraClient(),
                access_token="tok",
                cloud_id="cloud",
            )

            assets = session.query(KnowledgeAsset).order_by(KnowledgeAsset.source_type, KnowledgeAsset.source_ref).all()
            assert result.created_assets == 3
            assert result.updated_assets == 0
            assert result.deleted_assets == 0
            assert result.failed_assets == 0
            assert result.skipped_assets == 1
            assert [asset.source_type for asset in assets] == ["jira_attachment", "jira_comment", "jira_issue"]

            class _JiraClientUpdated(_JiraClient):
                def list_issue_comments(self, **_kwargs):
                    return [
                        SimpleNamespace(
                            comment_id="2001",
                            body="Production Bundle ID: com.route25.girlpower\nStaging Bundle ID: com.route25.girlpower.stage",
                            author_display_name="Alice",
                            updated_at=datetime(2026, 3, 10, 13, 0, tzinfo=timezone.utc),
                        )
                    ]

                def list_issue_attachments(self, **_kwargs):
                    return []

            updated = sync_project_knowledge_from_jira(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                project_key="GP",
                jira_client=_JiraClientUpdated(),
                access_token="tok",
                cloud_id="cloud",
            )

            active_assets = session.query(KnowledgeAsset).filter(KnowledgeAsset.status != "deleted").all()
            deleted_assets = session.query(KnowledgeAsset).filter(KnowledgeAsset.status == "deleted").all()
            assert updated.created_assets == 0
            assert updated.updated_assets >= 1
            assert updated.deleted_assets == 1
            assert updated.failed_assets == 0
            assert len(active_assets) == 2
            assert len(deleted_assets) == 1
            assert any(asset.source_type == "jira_comment" and "com.route25.girlpower.stage" in str(asset.text_content) for asset in active_assets)


def test_sync_project_knowledge_from_jira_handles_long_labeled_fact_lines() -> None:
    with TemporaryDirectory() as temp_dir:
        database_url = f"sqlite:///{temp_dir}/knowledge_sync_long_fact.db"
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        session_factory = create_session_factory(database_url)

        with session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-1",
                    name="Tenant",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config={},
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.add(
                Project(
                    project_id="project-1",
                    tenant_id="tenant-1",
                    name="Project",
                    github_repository="example/repo",
                    jira_project_key="GP",
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

            class _JiraClient:
                def search_issues_by_jql_page(self, **_kwargs):
                    return SimpleNamespace(issues=[SimpleNamespace(key="GP-122")], next_page_token=None)

                def get_issue_detail(self, **_kwargs):
                    return SimpleNamespace(
                        key="GP-122",
                        summary="Knowledge sync fact overflow",
                        status="To Do",
                        description=(
                            "Implemented GP-122 on branch run-gp-122-ed1554de-484b-46ed-831f-781376230734 "
                            "and opened PR https://github.com/example/repo/pull/7.: Success."
                        ),
                        labels=[],
                    )

                def list_issue_comments(self, **_kwargs):
                    return []

                def list_issue_attachments(self, **_kwargs):
                    return []

            result = sync_project_knowledge_from_jira(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                project_key="GP",
                jira_client=_JiraClient(),
                access_token="tok",
                cloud_id="cloud",
            )

            facts = session.query(KnowledgeFact).filter(KnowledgeFact.fact_type == "reference_fact").all()

        assert result.failed_assets == 0
        assert facts
        assert all(len(fact.slot_name) <= 128 for fact in facts)
        assert any(fact.slot_name == "reference_fact" for fact in facts)


def test_sync_project_knowledge_from_jira_with_pgvector_string_embeddings() -> None:
    from unittest.mock import patch

    with TemporaryDirectory() as temp_dir:
        database_url = f"sqlite:///{temp_dir}/knowledge_sync_pgvector_strings.db"
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        session_factory = create_session_factory(database_url)

        with session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-1",
                    name="Tenant",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config={},
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.add(
                Project(
                    project_id="project-1",
                    tenant_id="tenant-1",
                    name="Project",
                    github_repository="example/repo",
                    jira_project_key="GP",
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

            class _JiraClient:
                def search_issues_by_jql_page(self, **_kwargs):
                    return SimpleNamespace(issues=[SimpleNamespace(key="GP-122")], next_page_token=None)

                def get_issue_detail(self, **_kwargs):
                    return SimpleNamespace(
                        key="GP-122",
                        summary="Auth gate rollout",
                        status="Blocked",
                        description="Need Apple Sign In config confirmed.",
                        labels=[],
                    )

                def list_issue_comments(self, **_kwargs):
                    return []

                def list_issue_attachments(self, **_kwargs):
                    return []

            with patch(
                "orchestrator.core.knowledge.base._embed_texts",
                return_value=["[0.1,0.2,0.3]"],
            ):
                result = sync_project_knowledge_from_jira(
                    session=session,
                    tenant_id="tenant-1",
                    project_id="project-1",
                    project_key="GP",
                    jira_client=_JiraClient(),
                    access_token="tok",
                    cloud_id="cloud",
                )

            assert result.failed_assets == 0
