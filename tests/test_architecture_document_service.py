from __future__ import annotations

from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.projects.architecture_document_service import (
    ARCHITECTURE_DOC_STATUS_READY,
    ArchitectureDocumentService,
)
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Project, Tenant


def _build_session_factory(temp_dir: str):
    database_url = f"sqlite:///{temp_dir}/architecture_documents.db"
    reset_db_engine_cache()
    run_migrations(database_url=database_url)
    return create_session_factory(database_url)


def test_internal_architecture_document_gate_creates_draft_stub_and_blocks_until_ready() -> (
    None
):
    with TemporaryDirectory() as temp_dir:
        session_factory = _build_session_factory(temp_dir)
        with session_factory() as session:
            now = datetime.now(timezone.utc)
            tenant = Tenant(
                tenant_id="tenant-a",
                name="Tenant A",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                experience_config={},
                setup_state={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-a",
                tenant_id="tenant-a",
                name="Project A",
                github_repository="example/repo",
                jira_project_key="TP",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={},
                architecture_docs_config={"provider": "internal"},
                created_at=now,
                updated_at=now,
            )
            session.add(tenant)
            session.add(project)
            session.commit()

            service = ArchitectureDocumentService(
                settings_factory=lambda: SimpleNamespace(
                    admin_ui_base_url="http://localhost:4100"
                )
            )
            with patch(
                "orchestrator.core.knowledge.base._embed_texts", return_value=[]
            ):
                gate = service.resolve_gate(
                    session=session,
                    project=project,
                    parent_issue_key="TP-215",
                    issue_summary="Decision Engine v2",
                    issue_labels=["pm-parent", "architecture-required"],
                    actor="system",
                )

            assert gate.required is True
            assert gate.ready is False
            assert gate.document is not None
            assert gate.document.provider == "internal"
            assert gate.document.canonical_url.endswith(
                "/tenant-a/projects/project-a/architecture?documentId="
                + gate.document.document_id
            )

            updated = service.update_document(
                session=session,
                document=gate.document,
                title=gate.document.title,
                status_value=ARCHITECTURE_DOC_STATUS_READY,
                actor="user-1",
                content_markdown="# Decision Engine v2\n\n## Architecture Overview\n- Stable contract.",
            )
            ready_gate = service.resolve_gate(
                session=session,
                project=project,
                parent_issue_key="TP-215",
                issue_summary="Decision Engine v2",
                issue_labels=["pm-parent", "architecture-required"],
                actor="system",
            )

            assert updated.status == "ready"
            assert ready_gate.ready is True
            assert ready_gate.document is not None
            assert ready_gate.document.document_id == gate.document.document_id


def test_confluence_architecture_document_gate_creates_provider_stub_and_blocks_until_ready() -> (
    None
):
    with TemporaryDirectory() as temp_dir:
        session_factory = _build_session_factory(temp_dir)
        with session_factory() as session:
            now = datetime.now(timezone.utc)
            tenant = Tenant(
                tenant_id="tenant-b",
                name="Tenant B",
                is_enabled=True,
                jira_config={"connection_id": "conn-1"},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                experience_config={},
                setup_state={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-b",
                tenant_id="tenant-b",
                name="Project B",
                github_repository="example/repo",
                jira_project_key="TB",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={},
                architecture_docs_config={
                    "provider": "confluence",
                    "space_key": "ARCH",
                },
                created_at=now,
                updated_at=now,
            )
            session.add(tenant)
            session.add(project)
            session.commit()

            service = ArchitectureDocumentService(
                settings_factory=lambda: SimpleNamespace(
                    admin_ui_base_url="http://localhost:4100"
                )
            )

            fake_oauth = SimpleNamespace(
                access_token="token-1",
                connection=SimpleNamespace(
                    cloud_id="cloud-1",
                    site_url="https://example.atlassian.net",
                    scopes=["read:space:confluence", "write:page:confluence"],
                ),
                client=SimpleNamespace(
                    get_confluence_space_by_key=lambda **_kwargs: SimpleNamespace(
                        space_id="space-123",
                        key="ARCH",
                        name="Architecture",
                    ),
                    create_confluence_page=lambda **_kwargs: SimpleNamespace(
                        page_id="page-123",
                        title="TB-42: Confluence contract",
                        webui_url="https://example.atlassian.net/wiki/spaces/ARCH/pages/123",
                    ),
                    update_confluence_page_title=lambda **_kwargs: SimpleNamespace(
                        page_id="page-123",
                        title="TB-42: Confluence contract",
                        webui_url="https://example.atlassian.net/wiki/spaces/ARCH/pages/123",
                    ),
                ),
            )
            with patch(
                "orchestrator.core.projects.architecture_document_service.tenant_atlassian_oauth_context",
                return_value=fake_oauth,
            ):
                gate = service.resolve_gate(
                    session=session,
                    project=project,
                    parent_issue_key="TB-42",
                    issue_summary="Confluence contract",
                    issue_labels=["pm-parent", "architecture-required"],
                    actor="system",
                )

                assert gate.required is True
                assert gate.ready is False
                assert gate.document is not None
                assert gate.document.provider == "confluence"
                assert gate.document.provider_ref == "page-123"
                assert (
                    gate.document.canonical_url
                    == "https://example.atlassian.net/wiki/spaces/ARCH/pages/123"
                )

                updated = service.update_document(
                    session=session,
                    document=gate.document,
                    title="TB-42: Confluence contract",
                    status_value=ARCHITECTURE_DOC_STATUS_READY,
                    actor="user-1",
                )
                ready_gate = service.resolve_gate(
                    session=session,
                    project=project,
                    parent_issue_key="TB-42",
                    issue_summary="Confluence contract",
                    issue_labels=["pm-parent", "architecture-required"],
                    actor="system",
                )

            assert updated.status == "ready"
            assert ready_gate.ready is True
            assert ready_gate.document is not None
            assert ready_gate.document.document_id == gate.document.document_id


def test_confluence_architecture_document_requires_space_key_configuration() -> None:
    with TemporaryDirectory() as temp_dir:
        session_factory = _build_session_factory(temp_dir)
        with session_factory() as session:
            now = datetime.now(timezone.utc)
            tenant = Tenant(
                tenant_id="tenant-c",
                name="Tenant C",
                is_enabled=True,
                jira_config={"connection_id": "conn-1"},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                experience_config={},
                setup_state={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-c",
                tenant_id="tenant-c",
                name="Project C",
                github_repository="example/repo",
                jira_project_key="TC",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={},
                architecture_docs_config={"provider": "confluence"},
                created_at=now,
                updated_at=now,
            )
            session.add(tenant)
            session.add(project)
            session.commit()

            service = ArchitectureDocumentService(
                settings_factory=lambda: SimpleNamespace(
                    admin_ui_base_url="http://localhost:4100"
                )
            )
            try:
                service.resolve_gate(
                    session=session,
                    project=project,
                    parent_issue_key="TC-42",
                    issue_summary="Missing space key",
                    issue_labels=["pm-parent", "architecture-required"],
                    actor="system",
                )
                assert False, "expected missing space key failure"
            except Exception as exc:  # noqa: BLE001
                assert "has no space key" in str(exc)
