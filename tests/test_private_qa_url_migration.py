from alembic.config import Config
from alembic.script import ScriptDirectory
from copy import deepcopy
from pathlib import Path


def migration():
    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option(
        "script_location", str(Path("orchestrator/storage/migrations").resolve())
    )
    return ScriptDirectory.from_config(config).get_revision("20261002_0136").module


def test_qa_url_migration_uses_verified_scope_preserving_other_fields():
    module = migration()
    original = {
        "stages": {
            "qa": {
                "artifact": {
                    "outcome": "continue",
                    "recordings": [
                        {
                            "object_key": "tenant/project/run/video.webm",
                            "artifact_url": "https://old-storage.example/qa-demos/tenant/project/run/video.webm",
                            "name": "Example",
                            "content_sha256": "recording-hash",
                        }
                    ],
                    "failure_evidence": [],
                }
            },
            "pm": {"artifact": {"documentation_url": "https://public.example/doc"}},
        }
    }
    saved = deepcopy(original)
    updated, changed = module.migrate_snapshot(
        original,
        tenant_id="tenant",
        project_id="project",
        run_id="run",
        delivery_base_url="https://ui.example/api/bff/api/qa-artifacts",
    )
    assert changed and original == saved
    assert (
        updated["stages"]["qa"]["artifact"]["recordings"][0]["artifact_url"]
        == "https://ui.example/api/bff/api/qa-artifacts/tenant/project/run/video.webm"
    )
    assert updated["stages"]["pm"] == saved["stages"]["pm"]
    assert (
        updated["stages"]["qa"]["artifact"]["recordings"][0]["content_sha256"]
        == "recording-hash"
    )


def test_unverifiable_qa_keys_are_invalidated_without_guessing():
    module = migration()
    original = {
        "stages": {
            "qa": {
                "artifact": {
                    "outcome": "continue",
                    "recordings": [
                        {
                            "object_key": "foreign/project/run/video.webm",
                            "artifact_url": "https://old.example/video",
                        }
                    ],
                    "failure_evidence": [],
                }
            }
        }
    }
    updated, changed = module.migrate_snapshot(
        original,
        tenant_id="tenant",
        project_id="project",
        run_id="run",
        delivery_base_url="https://ui.example/api/bff/api/qa-artifacts",
    )
    artifact = updated["stages"]["qa"]["artifact"]
    assert changed and artifact["recordings"] == [] and artifact["outcome"] == "blocked"
    assert "recapture" in artifact["blocker_message"]
    assert "old.example" not in str(updated)


def test_qa_migration_requires_explicit_authenticated_delivery():
    import pytest

    module = migration()
    payload = {
        "stages": {
            "qa": {
                "artifact": {
                    "recordings": [
                        {
                            "object_key": "tenant/project/run/video.webm",
                            "artifact_url": "https://old.example/video",
                        }
                    ]
                }
            }
        }
    }
    with pytest.raises(RuntimeError, match="authenticated QA delivery"):
        module.migrate_snapshot(
            payload,
            tenant_id="tenant",
            project_id="project",
            run_id="run",
            delivery_base_url="",
        )


def test_actual_forward_migration_updates_run_and_checkpoint_json(monkeypatch):
    from datetime import datetime, timezone
    from alembic import command
    from orchestrator.core.config import get_settings
    from orchestrator.storage.db import create_session_factory
    from orchestrator.storage.migrations import run_migrations
    from orchestrator.storage.models import Tenant, Project, WorkflowCheckpoint, Run
    from tests.workflow_test_support import make_run, add_run_with_workflow

    now = datetime.now(timezone.utc)
    original = {
        "stages": {
            "qa": {
                "artifact": {
                    "outcome": "continue",
                    "recordings": [
                        {
                            "object_key": "tenant/project/run/video.webm",
                            "artifact_url": "https://old.example/video",
                        }
                    ],
                    "failure_evidence": [],
                }
            }
        }
    }
    with create_session_factory()() as session:
        session.add(
            Tenant(
                tenant_id="tenant",
                name="Example",
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            Project(
                project_id="project",
                tenant_id="tenant",
                name="Example",
                github_repository="example/repo",
                jira_project_key="EXAMPLE",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        run = make_run(
            run_id="run",
            tenant_id="tenant",
            project_id="project",
            issue_key="EXAMPLE-1",
            issue_summary="Example",
            created_at=now,
            plan=original,
        )
        add_run_with_workflow(session, run)
        session.flush()
        session.add(
            WorkflowCheckpoint(
                checkpoint_id="checkpoint",
                workflow_id=run.workflow_id,
                run_id="run",
                checkpoint_kind="execution",
                stage="qa",
                payload_json=original,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    settings = get_settings()
    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option(
        "script_location", str(Path("orchestrator/storage/migrations").resolve())
    )
    config.set_main_option("sqlalchemy.url", settings.database_url)
    config.attributes["configure_logger"] = False
    command.stamp(config, "20261002_0135")
    monkeypatch.setenv(
        "ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL",
        "https://ui.example/api/bff/api/qa-artifacts",
    )
    get_settings.cache_clear()
    run_migrations()
    with create_session_factory()() as session:
        for payload in (
            session.get(Run, "run").plan,
            session.get(WorkflowCheckpoint, "checkpoint").payload_json,
        ):
            assert (
                payload["stages"]["qa"]["artifact"]["recordings"][0]["artifact_url"]
                == "https://ui.example/api/bff/api/qa-artifacts/tenant/project/run/video.webm"
            )


def test_admission_migration_replay_rejects_unrecognized_existing_schema():
    import pytest
    from alembic import command
    from sqlalchemy import create_engine, text
    from orchestrator.core.config import get_settings
    from orchestrator.storage.migrations import run_migrations

    url = get_settings().database_url
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE auth_request_budgets ADD COLUMN unexpected_column INTEGER"
            )
        )
    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option(
        "script_location", str(Path("orchestrator/storage/migrations").resolve())
    )
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    command.stamp(config, "20261002_0134")
    with pytest.raises(RuntimeError, match="admission schema"):
        run_migrations()
    engine.dispose()
