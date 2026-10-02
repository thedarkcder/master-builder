from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import yaml

from orchestrator.core.config import get_settings
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project
from tests.workflow_test_support import add_run_with_workflow, make_run


def test_qa_bucket_is_private_with_retention_and_scoped_credentials():
    services = yaml.safe_load(Path("docker-compose.yml").read_text())["services"]
    from orchestrator.core.qa.storage_setup import identity_config

    init = services["seaweedfs-init"]
    assert init["build"]["dockerfile"] == "ops/seaweedfs/Dockerfile.init"
    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_RETENTION_DAYS" in init["environment"]
    assert ":?" in init["environment"]["SEAWEEDFS_ADMIN_SECRET_KEY"]
    identities = identity_config(bucket="qa-demos")
    application = identities["identities"][1]
    assert application["policyNames"] == ["qa-artifacts"]
    assert not application.get("actions")
    assert "s3:DeleteObject" not in identities["policies"][0]["content"]
    assert '"Principal"' not in identities["policies"][0]["content"]


def test_artifact_api_requires_authentication():
    from orchestrator.api.main import app

    response = TestClient(app).get("/api/qa-artifacts/tenant/project/run/video.webm")
    assert response.status_code == 401


def test_artifact_delivery_checks_actual_run_scope_and_streams_range(monkeypatch):
    from orchestrator.api.main import app

    monkeypatch.setenv("ORCHESTRATOR_PUBLIC_REGISTRATION_ENABLED", "true")
    monkeypatch.setenv(
        "ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT", "storage.example.invalid"
    )
    monkeypatch.setenv("ORCHESTRATOR_QA_DEMO_ARTIFACT_ACCESS_KEY", "example-access-key")
    monkeypatch.setenv("ORCHESTRATOR_QA_DEMO_ARTIFACT_SECRET_KEY", "example-secret")
    monkeypatch.setenv("ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET", "qa-demos")
    monkeypatch.setenv(
        "ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL",
        "http://localhost:60002/api/bff/api/qa-artifacts",
    )
    get_settings.cache_clear()
    client = TestClient(app)

    def register(email):
        result = client.post(
            "/api/public/register",
            json=dict(
                email=email,
                full_name="Example",
                password="example-password-only",
                tenant_name=email.split("@")[0],
            ),
        )
        assert result.status_code == 201, result.text
        return result.json()

    owner = register("owner@example.invalid")
    outsider = register("outsider@example.invalid")
    tenant_id = owner["tenant"]["tenant_id"]
    with create_session_factory()() as session:
        project_id = "artifact-project"
        project = Project(
            project_id=project_id,
            tenant_id=tenant_id,
            name="Artifact project",
            github_repository="example/artifacts",
            jira_project_key="EXAMPLE",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(project)
        session.flush()
        run = make_run(
            run_id="artifact-run",
            tenant_id=tenant_id,
            project_id=project_id,
            issue_key="EXAMPLE-1",
            issue_summary="Example",
            created_at=datetime.now(timezone.utc),
        )
        add_run_with_workflow(session, run)
        session.commit()
    object_body = Mock()
    object_body.stream.return_value = iter([b"abc"])
    storage = Mock()
    storage.stat_object.return_value = SimpleNamespace(
        size=6, content_type="video/webm"
    )
    storage.get_object.return_value = object_body
    url = f"/api/qa-artifacts/{tenant_id}/{project_id}/artifact-run/video.webm"
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    with patch("minio.Minio", return_value=storage):
        denied = client.get(
            url, headers={"Authorization": f"Bearer {outsider['access_token']}"}
        )
        assert denied.status_code in (403, 404)
        storage.stat_object.assert_not_called()
        wrong = client.get(
            url.replace(f"/{project_id}/", "/foreign-project/"), headers=headers
        )
        assert wrong.status_code == 404
        storage.stat_object.assert_not_called()
        response = client.get(url, headers={**headers, "Range": "bytes=0-2"})
        assert response.status_code == 206, response.text
        assert response.content == b"abc"
        assert response.headers["content-range"] == "bytes 0-2/6"
        assert response.headers["cache-control"] == "private, no-store"
        storage.get_object.assert_called_once_with(
            "qa-demos",
            f"{tenant_id}/{project_id}/artifact-run/video.webm",
            offset=0,
            length=3,
        )
        object_body.close.assert_called_once()
        object_body.release_conn.assert_called_once()
        assert (
            client.get(url, headers={**headers, "Range": "bytes=0-1,3-4"}).status_code
            == 416
        )


def test_worker_probe_authenticates_storage_and_never_opens_public_url(monkeypatch):
    from orchestrator.core.qa.demo_service import ensure_artifact_url_reachable

    monkeypatch.setenv(
        "ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT", "storage.example.invalid"
    )
    monkeypatch.setenv("ORCHESTRATOR_QA_DEMO_ARTIFACT_ACCESS_KEY", "example-access-key")
    monkeypatch.setenv("ORCHESTRATOR_QA_DEMO_ARTIFACT_SECRET_KEY", "example-secret")
    monkeypatch.setenv("ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET", "qa-demos")
    monkeypatch.setenv(
        "ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL",
        "https://ui.example/api/bff/api/qa-artifacts",
    )
    get_settings.cache_clear()
    with (
        patch("minio.Minio") as constructor,
        patch("urllib.request.urlopen") as anonymous,
    ):
        constructor.return_value.stat_object.return_value = SimpleNamespace(size=6)
        ensure_artifact_url_reachable(
            "https://ui.example/api/bff/api/qa-artifacts/tenant/project/run/video.webm"
        )
        constructor.return_value.stat_object.assert_called_once_with(
            "qa-demos", "tenant/project/run/video.webm"
        )
        anonymous.assert_not_called()


def test_artifact_byte_range_rejects_unbounded_integer_input():
    import pytest
    from fastapi import HTTPException
    from orchestrator.api.routes.qa_artifacts import _range

    for value in ("bytes=" + "9" * 5000 + "-", "bytes=-" + "9" * 5000):
        with pytest.raises(HTTPException) as error:
            _range(value, 6)
        assert error.value.status_code == 416


def test_qa_storage_timeout_requires_explicit_finite_positive_value():
    import pytest
    from orchestrator.core.qa.demo_service import qa_demo_artifact_url_timeout_seconds

    for settings in (
        SimpleNamespace(),
        *(
            SimpleNamespace(qa_demo_artifact_url_timeout_seconds=value)
            for value in (None, "invalid", 0, -1, float("nan"), float("inf"))
        ),
    ):
        with pytest.raises(RuntimeError, match="finite positive"):
            qa_demo_artifact_url_timeout_seconds(settings)
    assert (
        qa_demo_artifact_url_timeout_seconds(
            SimpleNamespace(qa_demo_artifact_url_timeout_seconds=0.25)
        )
        == 0.25
    )
