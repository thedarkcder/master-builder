from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import select

from orchestrator.core.binding_resolution_service import check_project_bindings
from orchestrator.core.config import get_settings
from orchestrator.core.install_request_service import (
    INSTALL_REQUEST_KIND_PROJECT_MISSING,
    create_install_request,
    ProjectInstallRequestWrite,
)
from orchestrator.core.trusted_install_executor import run_install
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Project, RunHumanInputRequest, Tenant, WorkflowExecution
from tests.workflow_test_support import add_run_with_workflow, make_run


class TestProjectInstallServices:
    def setup_method(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/project_install_services.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(self.database_url)

    def teardown_method(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)
        get_settings.cache_clear()
        reset_db_engine_cache()

    def test_create_install_request_persists_request_and_pauses_workflow(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-a",
                name="Tenant A",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-a",
                tenant_id="tenant-a",
                name="Project A",
                github_repository="https://github.com/example/repo",
                jira_project_key="PA",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={},
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            run = make_run(
                run_id="run-1",
                tenant_id="tenant-a",
                project_id="project-a",
                issue_key="PA-1",
                issue_summary="Needs Fastlane",
                created_at=now,
                status="running",
            )
            session.add(tenant)
            session.add(project)
            add_run_with_workflow(session, run, workflow_status="running")
            session.commit()
            settings = get_settings()

            with patch("orchestrator.core.run_human_input_service._dispatch_human_input_request"):
                request = create_install_request(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    project=project,
                    run=run,
                    source_stage="dev",
                    payload=ProjectInstallRequestWrite(
                        kind="fastlane_lane",
                        label="iOS Beta Lane",
                        reason="Ticket requires Fastlane",
                        suggested_config={"working_dir": ".", "platform": "ios", "lane": "beta"},
                        required_bindings=("MATCH_PASSWORD",),
                    ),
                )

            persisted_run = session.get(type(run), run.run_id)
            workflow = session.get(WorkflowExecution, run.workflow_id)
            human_request = session.execute(select(RunHumanInputRequest)).scalar_one()

        assert request.request_kind == INSTALL_REQUEST_KIND_PROJECT_MISSING
        assert request.status == "pending"
        assert persisted_run is not None and persisted_run.status == "waiting_for_input"
        assert workflow is not None and workflow.status == "waiting_for_input"
        assert human_request is not None
        assert human_request.request_type == "install_request"

    def test_binding_status_reports_secret_ref_source_without_value(self) -> None:
        project = SimpleNamespace(
            project_id="project-a",
            environment={"SUPABASE_URL": "https://example.supabase.co"},
            secret_refs={"SUPABASE_ANON_KEY": "tenant/tenant-a/SUPABASE_ANON_KEY"},
        )
        with patch("orchestrator.core.binding_resolution_service.resolve_scoped_secret_ref", return_value=None):
            statuses = check_project_bindings(
                session=object(),  # type: ignore[arg-type]
                project=project,  # type: ignore[arg-type]
                tenant_id="tenant-a",
                encryption_key="enc-key",
                keys=["SUPABASE_URL", "SUPABASE_ANON_KEY", "MISSING_KEY"],
            )

        assert [(item.key, item.present, item.source) for item in statuses] == [
            ("SUPABASE_URL", True, "environment"),
            ("SUPABASE_ANON_KEY", False, "secret_ref"),
            ("MISSING_KEY", False, "missing"),
        ]

    def test_run_install_redacts_echoed_binding_values(self) -> None:
        with TemporaryDirectory() as repo_dir:
            install = SimpleNamespace(
                install_id="install-1",
                tenant_id="tenant-a",
                project_id="project-a",
                enabled=True,
                kind="fastlane_lane",
                label="iOS Beta Lane",
                config_json={"working_dir": ".", "platform": "ios", "lane": "beta", "use_bundle_exec": True},
                binding_names_json=["MATCH_PASSWORD"],
            )
            project = SimpleNamespace(project_id="project-a", tenant_id="tenant-a")

            with (
                patch(
                    "orchestrator.core.trusted_install_executor.resolve_project_binding_values",
                    return_value={"MATCH_PASSWORD": "super-secret-value"},
                ),
                patch(
                    "orchestrator.core.trusted_install_executor.subprocess.run",
                    return_value=SimpleNamespace(
                        returncode=0,
                        stdout="MATCH_PASSWORD=super-secret-value\nfinished",
                        stderr="",
                    ),
                ),
            ):
                payload = run_install(
                    session=object(),  # type: ignore[arg-type]
                    settings=SimpleNamespace(secrets_encryption_key="enc-key"),
                    project=project,  # type: ignore[arg-type]
                    install=install,  # type: ignore[arg-type]
                    repo_dir=Path(repo_dir),
                )

        assert payload["ok"] is True
        assert "super-secret-value" not in payload["stdout"]
        assert "[REDACTED]" in payload["stdout"]
