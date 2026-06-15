from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator.core.worker.capability_normalization import WorkerCapability
from orchestrator.core.qa.demo_service import (
    DEMO_EVIDENCE_HEADING,
    DEMO_EVIDENCE_MARKER,
    DEMO_EVIDENCE_REQUIRED_COUNTS_MARKER,
    DEMO_EVIDENCE_REQUIRED_TARGETS_MARKER,
    DemoCaptureTarget,
    ensure_artifact_url_reachable,
    ensure_release_ready_for_qa,
    ensure_capture_target_runtime_ready,
    execute_qa_demo_stage,
    planned_capture_target_constraints_payload,
    next_required_qa_demo_worker_capability,
    qa_demo_max_attempts,
    qa_demo_recording_enabled,
    qa_demo_recorder_process_timeout_seconds,
    record_demo_scenarios,
    remaining_capture_targets,
    release_context_sha256_for_release,
    required_capture_targets,
    required_recording_counts_by_target,
    required_release_service_kinds,
    resolve_available_capture_targets,
    resolve_preview_demo_url,
    storage_config_from_settings,
    update_pull_request_with_demo_evidence,
    upload_recording,
    upsert_demo_evidence_section,
)
from orchestrator.core.workflow.runner import (
    DemoRequirement,
    DevResult,
    PmPlan,
    QaRecording,
    QaResult,
    QaScenario,
    QaStep,
    ReviewResult,
    TestResult,
    WorkflowRequest,
)


def _request() -> WorkflowRequest:
    return WorkflowRequest(
        tenant_id="tenant-1",
        project_id="project-1",
        run_id="run-1",
        issue_key="MAB-400",
        issue_summary="Add QA demos",
        issue_description="desc",
        max_dev_test_review_loops=1,
        execution_repo_dir="/tmp/repo",
        execution_branch="run/MAB-400/run-1",
        integration_branch="feature/MAB-400",
        base_branch="main",
        pr_target_branch="main",
    )


def _qa_artifact_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "secrets_encryption_key": "",
        "qa_demo_artifact_endpoint": "127.0.0.1:9000",
        "qa_demo_artifact_access_key": "minio",
        "qa_demo_artifact_secret_key": "minio-secret",
        "qa_demo_artifact_bucket": "qa-demos",
        "qa_demo_artifact_public_base_url": "https://cdn.example/qa-demos",
        "qa_demo_artifact_secure": False,
        "qa_demo_artifact_url_timeout_seconds": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _qa_result() -> QaResult:
    return QaResult(
        summary=["Recorded demos"],
        scenarios=[
            QaScenario(
                name="Happy path",
                objective="Show feature works",
                steps=[QaStep(action="goto", value="/"), QaStep(action="assert_visible", selector="text=Feature")],
            ),
            QaScenario(
                name="Repeat action remains safe",
                objective="Show repeat action remains safe",
                steps=[QaStep(action="goto", value="/"), QaStep(action="assert_visible", selector="text=Feature")],
            )
        ],
    )


def _demo_requirement(
    *,
    title: str = "Feature walkthrough",
    acceptance_criterion: str = "Feature works",
    capture_target: str = "browser",
    variants: list[str] | None = None,
) -> DemoRequirement:
    return DemoRequirement(
        title=title,
        acceptance_criterion=acceptance_criterion,
        capture_target=capture_target,  # type: ignore[arg-type]
        variants=variants or ["Repeat action remains safe"],
    )


def _local_recording(
    *,
    name: str,
    path: str = "/tmp/happy.webm",
    capture_target: str = "browser",
    capture_reference: str = "https://preview.example",
    content_type: str = "video/webm",
    content_sha256: str = "0" * 64,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        path=path,
        capture_target=capture_target,
        capture_reference=capture_reference,
        content_type=content_type,
        content_sha256=content_sha256,
    )


def _browser_capture_targets() -> dict[str, DemoCaptureTarget]:
    return {
        "browser": DemoCaptureTarget(
            capture_target="browser",
            capture_reference="https://preview.example",
        )
    }


def _proof_steps(selector: str = "text=Feature") -> list[QaStep]:
    return [QaStep(action="assert_visible", selector=selector)]


def _fake_webm_payload() -> bytes:
    return b"\x1a\x45\xdf\xa3" + (b"webm-video-evidence" * 128)


def _fake_mp4_payload() -> bytes:
    return b"\x00\x00\x00\x18ftypmp42" + (b"mp4-video-evidence" * 128) + b"moov"


def _sha256(index: int) -> str:
    return f"{index:064x}"


def _release_context_sha256(*, include_api: bool = False) -> str:
    website_service_name = "web" if include_api else ""
    urls = [{"service_kind": "website", "service_name": website_service_name, "url": "https://preview.example"}]
    if include_api:
        urls.insert(0, {"service_kind": "api", "service_name": "api", "url": "https://api.preview.example"})
    return release_context_sha256_for_release(
        release=SimpleNamespace(commit_sha="b" * 40),
        release_service_urls=urls,
    )


def _release_context_sha256_with_commit(*, include_api: bool = False, commit_sha: str = "b" * 40) -> str:
    website_service_name = "web" if include_api else ""
    urls = [{"service_kind": "website", "service_name": website_service_name, "url": "https://preview.example"}]
    if include_api:
        urls.insert(0, {"service_kind": "api", "service_name": "api", "url": "https://api.preview.example"})
    return release_context_sha256_for_release(
        release=SimpleNamespace(commit_sha=commit_sha),
        release_service_urls=urls,
    )


def _empty_release_context_sha256() -> str:
    return release_context_sha256_for_release(
        release=SimpleNamespace(commit_sha="b" * 40),
        release_service_urls=[],
    )


def _preview_release(*, service_urls: list[object] | None = None, commit_sha: str = "b" * 40) -> SimpleNamespace:
    return SimpleNamespace(commit_sha=commit_sha, service_urls=list(service_urls or []))


def test_qa_demo_recording_enabled_reads_effective_policy() -> None:
    assert qa_demo_recording_enabled({"qa_demo_recording_enabled": True}) is True
    assert qa_demo_recording_enabled({"qa_demo_recording_enabled": False}) is False
    assert qa_demo_recording_enabled({}) is False


def test_qa_demo_max_attempts_defaults_and_caps() -> None:
    assert qa_demo_max_attempts(SimpleNamespace()) == 3
    assert qa_demo_max_attempts(SimpleNamespace(qa_demo_max_attempts=0)) == 1
    assert qa_demo_max_attempts(SimpleNamespace(qa_demo_max_attempts="5")) == 5


def test_qa_demo_recorder_process_timeout_defaults_and_caps() -> None:
    assert qa_demo_recorder_process_timeout_seconds(SimpleNamespace()) == 900.0
    assert qa_demo_recorder_process_timeout_seconds(SimpleNamespace(qa_demo_recorder_process_timeout_seconds=0)) == 1.0
    assert qa_demo_recorder_process_timeout_seconds(SimpleNamespace(qa_demo_recorder_process_timeout_seconds="123.5")) == 123.5


def test_storage_config_from_settings_requires_complete_configuration() -> None:
    try:
        storage_config_from_settings(SimpleNamespace())
    except RuntimeError as exc:
        assert "not fully configured" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected storage config validation failure")


def test_upload_recording_persists_and_verifies_content_sha256_metadata(monkeypatch) -> None:
    calls: dict[str, object] = {}
    expected_digest = _sha256(47)
    expected_release_context_digest = _release_context_sha256()

    class FakeMinio:
        def __init__(self, endpoint: str, *, access_key: str, secret_key: str, secure: bool) -> None:
            calls["init"] = {
                "endpoint": endpoint,
                "access_key": access_key,
                "secret_key": secret_key,
                "secure": secure,
            }

        def bucket_exists(self, bucket: str) -> bool:
            calls["bucket_exists"] = bucket
            return True

        def fput_object(
            self,
            bucket: str,
            object_key: str,
            local_path: str,
            *,
            content_type: str,
            metadata: dict[str, str],
        ) -> None:
            calls["fput_object"] = {
                "bucket": bucket,
                "object_key": object_key,
                "local_path": local_path,
                "content_type": content_type,
                "metadata": metadata,
            }

        def stat_object(self, bucket: str, object_key: str) -> SimpleNamespace:
            calls["stat_object"] = {"bucket": bucket, "object_key": object_key}
            return SimpleNamespace(
                metadata={
                    "X-Amz-Meta-Content-Sha256": expected_digest,
                    "X-Amz-Meta-Release-Context-Sha256": expected_release_context_digest,
                }
            )

    monkeypatch.setitem(sys.modules, "minio", SimpleNamespace(Minio=FakeMinio))

    artifact_url = upload_recording(
        storage=storage_config_from_settings(_qa_artifact_settings()),
        local_path="/tmp/demo.webm",
        object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
        content_type="video/webm",
        content_sha256=expected_digest,
        release_context_sha256=expected_release_context_digest,
    )

    assert artifact_url == "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm"
    assert calls["fput_object"] == {
        "bucket": "qa-demos",
        "object_key": "tenant-1/project-1/run-1/qa-demo-1.webm",
        "local_path": "/tmp/demo.webm",
        "content_type": "video/webm",
        "metadata": {
            "content-sha256": expected_digest,
            "release-context-sha256": expected_release_context_digest,
        },
    }
    assert calls["stat_object"] == {
        "bucket": "qa-demos",
        "object_key": "tenant-1/project-1/run-1/qa-demo-1.webm",
    }


def test_upload_recording_rejects_storage_metadata_digest_mismatch(monkeypatch) -> None:
    class FakeMinio:
        def __init__(self, endpoint: str, *, access_key: str, secret_key: str, secure: bool) -> None:
            pass

        def bucket_exists(self, bucket: str) -> bool:
            return True

        def fput_object(
            self,
            bucket: str,
            object_key: str,
            local_path: str,
            *,
            content_type: str,
            metadata: dict[str, str],
        ) -> None:
            pass

        def stat_object(self, bucket: str, object_key: str) -> SimpleNamespace:
            return SimpleNamespace(metadata={"X-Amz-Meta-Content-Sha256": _sha256(49)})

    monkeypatch.setitem(sys.modules, "minio", SimpleNamespace(Minio=FakeMinio))

    try:
        upload_recording(
            storage=storage_config_from_settings(_qa_artifact_settings()),
            local_path="/tmp/demo.webm",
            object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
            content_type="video/webm",
            content_sha256=_sha256(48),
            release_context_sha256=_release_context_sha256(),
        )
    except RuntimeError as exc:
        assert "QA demo artifact metadata sha256 mismatch after upload" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected storage metadata digest mismatch to block upload")


def test_upload_recording_rejects_storage_release_context_metadata_digest_mismatch(monkeypatch) -> None:
    expected_digest = _sha256(48)

    class FakeMinio:
        def __init__(self, endpoint: str, *, access_key: str, secret_key: str, secure: bool) -> None:
            pass

        def bucket_exists(self, bucket: str) -> bool:
            return True

        def fput_object(
            self,
            bucket: str,
            object_key: str,
            local_path: str,
            *,
            content_type: str,
            metadata: dict[str, str],
        ) -> None:
            pass

        def stat_object(self, bucket: str, object_key: str) -> SimpleNamespace:
            return SimpleNamespace(
                metadata={
                    "X-Amz-Meta-Content-Sha256": expected_digest,
                    "X-Amz-Meta-Release-Context-Sha256": _sha256(49),
                }
            )

    monkeypatch.setitem(sys.modules, "minio", SimpleNamespace(Minio=FakeMinio))

    try:
        upload_recording(
            storage=storage_config_from_settings(_qa_artifact_settings()),
            local_path="/tmp/demo.webm",
            object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
            content_type="video/webm",
            content_sha256=expected_digest,
            release_context_sha256=_release_context_sha256(),
        )
    except RuntimeError as exc:
        assert "QA demo artifact metadata release context sha256 mismatch after upload" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected storage release context metadata digest mismatch to block upload")


def test_resolve_preview_demo_url_prefers_active_website() -> None:
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[
            SimpleNamespace(service_kind="api", status="active", url="https://api.example"),
            SimpleNamespace(service_kind="website", status="active", url="https://preview.example"),
        ]
    )
    assert resolve_preview_demo_url(release) == "https://preview.example"


def test_resolve_preview_demo_url_rejects_inactive_website() -> None:
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[
            SimpleNamespace(service_kind="api", status="active", url="https://api.example"),
            SimpleNamespace(service_kind="website", status="pending", url="https://preview.example"),
        ]
    )

    try:
        resolve_preview_demo_url(release)
    except RuntimeError as exc:
        assert "active preview website URL" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected inactive browser preview URL to be rejected")


def test_required_capture_targets_preserves_pm_order() -> None:
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[
            DemoRequirement(title="Web walkthrough", acceptance_criterion="Feature works", capture_target="browser"),
            DemoRequirement(title="iOS walkthrough", acceptance_criterion="iOS flow works", capture_target="ios"),
            DemoRequirement(title="Android walkthrough", acceptance_criterion="Android flow works", capture_target="android"),
            DemoRequirement(title="Desktop walkthrough", acceptance_criterion="Desktop flow works", capture_target="desktop"),
            DemoRequirement(title="Browser retry", acceptance_criterion="Feature works", capture_target="browser"),
        ],
    )

    assert required_capture_targets(plan) == ("browser", "ios", "android", "desktop")


def test_remaining_capture_targets_requires_recording_per_requirement_and_variant() -> None:
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[
            DemoRequirement(
                title="Browser walkthrough",
                acceptance_criterion="Feature works",
                capture_target="browser",
                variants=["Bad input shows validation", "Repeat action remains safe"],
            ),
            DemoRequirement(title="iOS walkthrough", acceptance_criterion="Feature works on iOS", capture_target="ios"),
        ],
    )

    assert required_recording_counts_by_target(plan) == {"browser": 3, "ios": 1}
    assert remaining_capture_targets(
        plan,
        [
            QaRecording(
                name="Browser happy path",
                artifact_url="https://cdn.example/browser-1.webm",
                object_key="tenant/project/run/browser-1.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(1),
                release_context_sha256=_release_context_sha256(),
            ),
            QaRecording(
                name="iOS walkthrough",
                artifact_url="https://cdn.example/ios-1.mp4",
                object_key="tenant/project/run/ios-1.mp4",
                capture_target="ios",
                capture_reference="ios-simulator://configured",
                content_sha256=_sha256(2),
                release_context_sha256=_release_context_sha256(),
            ),
        ],
    ) == ("browser",)
    assert (
        remaining_capture_targets(
            plan,
            [
                QaRecording(
                    name="Browser happy path",
                    artifact_url="https://cdn.example/browser-1.webm",
                    object_key="tenant/project/run/browser-1.webm",
                    capture_target="browser",
                    capture_reference="https://preview.example",
                    content_sha256=_sha256(3),
                    release_context_sha256=_release_context_sha256(),
                ),
                QaRecording(
                    name="Browser invalid input",
                    artifact_url="https://cdn.example/browser-2.webm",
                    object_key="tenant/project/run/browser-2.webm",
                    capture_target="browser",
                    capture_reference="https://preview.example",
                    content_sha256=_sha256(4),
                    release_context_sha256=_release_context_sha256(),
                ),
                QaRecording(
                    name="Browser repeat action",
                    artifact_url="https://cdn.example/browser-3.webm",
                    object_key="tenant/project/run/browser-3.webm",
                    capture_target="browser",
                    capture_reference="https://preview.example",
                    content_sha256=_sha256(5),
                    release_context_sha256=_release_context_sha256(),
                ),
                QaRecording(
                    name="iOS walkthrough",
                    artifact_url="https://cdn.example/ios-1.mp4",
                    object_key="tenant/project/run/ios-1.mp4",
                    capture_target="ios",
                    capture_reference="ios-simulator://configured",
                    content_sha256=_sha256(6),
                    release_context_sha256=_release_context_sha256(),
                ),
            ],
        )
        == ()
    )


def test_resolve_available_capture_targets_includes_configured_native_recorders() -> None:
    targets = resolve_available_capture_targets(
        settings=SimpleNamespace(
            qa_demo_ios_recorder_command="python /tmp/ios_recorder.py",
            qa_demo_ios_capture_reference="ios-simulator://default",
            qa_demo_android_recorder_command="python /tmp/android_recorder.py",
            qa_demo_android_capture_reference="android-emulator://default",
            qa_demo_desktop_recorder_command="python /tmp/desktop_recorder.py",
            qa_demo_desktop_capture_reference="desktop://macos-app",
            qa_demo_desktop_worker_platform="macos",
        ),
        preview_release=_preview_release(),
    )

    assert set(targets) == {"ios", "android", "desktop"}
    assert targets["ios"].required_worker_platform == "macos"
    assert targets["android"].required_worker_platform == "linux"
    assert targets["android"].recorder_command == ("python", "/tmp/android_recorder.py")
    assert targets["desktop"].recorder_command == ("python", "/tmp/desktop_recorder.py")
    assert targets["desktop"].required_worker_platform == "macos"


def test_resolve_available_capture_targets_allows_configured_android_worker_platform() -> None:
    targets = resolve_available_capture_targets(
        settings=SimpleNamespace(
            qa_demo_android_recorder_command="python /tmp/android_recorder.py",
            qa_demo_android_worker_platform="macos",
        ),
        preview_release=_preview_release(),
    )

    assert targets["android"].required_worker_platform == "macos"


def test_resolve_available_capture_targets_rejects_invalid_android_worker_platform() -> None:
    try:
        resolve_available_capture_targets(
            settings=SimpleNamespace(
                qa_demo_android_recorder_command="python /tmp/android_recorder.py",
                qa_demo_android_worker_platform="windows",
            ),
            preview_release=_preview_release(),
        )
    except RuntimeError as exc:
        assert "android worker platform is invalid" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected invalid android worker platform failure")


def test_resolve_available_capture_targets_rejects_invalid_desktop_worker_platform() -> None:
    try:
        resolve_available_capture_targets(
            settings=SimpleNamespace(
                qa_demo_desktop_recorder_command="python /tmp/desktop_recorder.py",
                qa_demo_desktop_worker_platform="windows",
            ),
            preview_release=_preview_release(),
        )
    except RuntimeError as exc:
        assert "desktop worker platform is invalid" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected invalid desktop worker platform failure")


def test_resolve_available_capture_targets_includes_builtin_ios_and_android_recorders_when_unconfigured() -> None:
    targets = resolve_available_capture_targets(
        settings=SimpleNamespace(),
        preview_release=_preview_release(),
    )

    assert "ios" in targets
    assert targets["ios"].recorder_command is not None
    assert targets["ios"].recorder_command[0] == sys.executable
    assert targets["ios"].recorder_command[-1].endswith("scripts/qa_demo_mobile_recorder.py")
    assert targets["ios"].required_worker_platform == "macos"
    assert "android" in targets
    assert targets["android"].recorder_command is not None
    assert targets["android"].recorder_command[0] == sys.executable
    assert targets["android"].recorder_command[-1].endswith("scripts/qa_demo_android_recorder.py")
    assert targets["android"].required_worker_platform == "linux"


def test_planned_capture_target_constraints_payload_marks_native_targets_available_from_provider_metadata(monkeypatch) -> None:
    def _unexpected_runtime_probe(*_args, **_kwargs):  # noqa: ANN001
        raise AssertionError("PM capture constraints must not probe current host native runtime")

    monkeypatch.setattr("orchestrator.core.qa.demo_service.subprocess.run", _unexpected_runtime_probe)
    payload = planned_capture_target_constraints_payload(settings=SimpleNamespace())
    by_target = {item["capture_target"]: item for item in payload}

    assert by_target["browser"]["provider_available"] is True
    assert by_target["ios"]["provider_available"] is True
    assert by_target["ios"]["required_worker_platform"] == "macos"
    assert by_target["android"]["provider_available"] is True
    assert by_target["android"]["required_worker_platform"] == "linux"
    assert by_target["desktop"]["provider_available"] is False
    assert "no desktop recorder command is configured" in str(by_target["desktop"]["availability_reason"])


def test_planned_capture_target_constraints_payload_uses_configured_android_worker_platform() -> None:
    payload = planned_capture_target_constraints_payload(
        settings=SimpleNamespace(
            qa_demo_android_recorder_command="python /tmp/android_recorder.py",
            qa_demo_android_worker_platform="macos",
        )
    )
    by_target = {item["capture_target"]: item for item in payload}

    assert by_target["android"]["provider_available"] is True
    assert by_target["android"]["required_worker_platform"] == "macos"
    assert "macOS Android worker" in str(by_target["android"]["availability_reason"])


def test_next_required_qa_demo_worker_capability_uses_configured_android_worker_platform() -> None:
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Android flow works"],
        risks=[],
        demo_requirements=[
            DemoRequirement(
                title="Android walkthrough",
                acceptance_criterion="Android flow works",
                capture_target="android",
            ),
        ],
    )

    assert next_required_qa_demo_worker_capability(
        settings=SimpleNamespace(
            qa_demo_android_recorder_command="python /tmp/android_recorder.py",
            qa_demo_android_worker_platform="macos",
        ),
        plan=plan,
        recordings=[],
    ) == WorkerCapability.MACOS


def test_ios_builtin_capture_runtime_requires_available_simulator(monkeypatch) -> None:
    target = DemoCaptureTarget(
        capture_target="ios",
        capture_reference="ios-simulator://configured",
        recorder_command=(sys.executable, "scripts/qa_demo_mobile_recorder.py"),
        required_worker_platform="macos",
    )

    def _fake_run(args, **_kwargs):  # noqa: ANN001
        assert args == ["xcrun", "simctl", "list", "devices", "available"]
        return SimpleNamespace(stdout="== Devices ==\n")

    monkeypatch.setattr("orchestrator.core.qa.demo_service.subprocess.run", _fake_run)

    try:
        ensure_capture_target_runtime_ready(target)
    except RuntimeError as exc:
        assert "No available iPhone simulator" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected iOS capture runtime readiness failure")


def test_ensure_release_ready_for_qa_requires_active_urls_for_required_services() -> None:
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[
            SimpleNamespace(service_kind="website", status="active", url="https://preview.example"),
            SimpleNamespace(service_kind="api", status="pending", url="https://api.example"),
        ]
    )

    try:
        ensure_release_ready_for_qa(release, required_service_kinds=("website", "api"))
    except RuntimeError as exc:
        assert "api" in str(exc)
        assert "not active" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected release readiness failure")


def test_ensure_release_ready_for_qa_rejects_unreachable_active_service_url() -> None:
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[
            SimpleNamespace(service_kind="website", status="active", url="https://preview.example"),
            SimpleNamespace(service_kind="api", status="active", url="https://api.example"),
        ]
    )

    def _probe(url: str, *, timeout_seconds: float) -> int:
        assert timeout_seconds == 7.0
        if url == "https://api.example":
            raise RuntimeError("connection refused")
        return 200

    try:
        ensure_release_ready_for_qa(
            release,
            required_service_kinds=("website", "api"),
            service_url_probe=_probe,
            timeout_seconds=7.0,
        )
    except RuntimeError as exc:
        assert "api" in str(exc)
        assert "connection refused" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected live release readiness failure")


def test_ensure_release_ready_for_qa_rejects_server_error_service_response() -> None:
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )

    try:
        ensure_release_ready_for_qa(
            release,
            required_service_kinds=("website",),
            service_url_probe=lambda _url, *, timeout_seconds: 503,
        )
    except RuntimeError as exc:
        assert "website" in str(exc)
        assert "HTTP 503" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected server error readiness failure")


def test_ensure_release_ready_for_qa_rejects_not_found_service_response() -> None:
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[SimpleNamespace(service_kind="api", status="active", url="https://api.example")]
    )

    try:
        ensure_release_ready_for_qa(
            release,
            required_service_kinds=("api",),
            service_url_probe=lambda _url, *, timeout_seconds: 404,
        )
    except RuntimeError as exc:
        assert "api" in str(exc)
        assert "HTTP 404" in str(exc)
        assert "not reachable" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected not-found readiness failure")


def test_ensure_release_ready_for_qa_allows_protected_running_service_response() -> None:
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[SimpleNamespace(service_kind="api", status="active", url="https://api.example")]
    )

    ensure_release_ready_for_qa(
        release,
        required_service_kinds=("api",),
        service_url_probe=lambda _url, *, timeout_seconds: 401,
    )


def test_required_release_service_kinds_prefers_release_deployment_snapshot() -> None:
    project = SimpleNamespace(
        deployment_config={
            "services": [
                {"kind": "website"},
            ]
        }
    )
    release = SimpleNamespace(
        deployment_snapshot={
            "services": [
                {"kind": "website"},
                {"kind": "api"},
                {"kind": "worker"},
                {"kind": "api"},
                {"kind": "website", "public": False},
            ]
        },
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")],
    )

    assert required_release_service_kinds(project=project, preview_release=release) == ("website", "api")


def test_required_release_service_kinds_uses_project_deployment_services_before_present_release_urls() -> None:
    project = SimpleNamespace(
        deployment_config={
            "services": [
                {"kind": "website"},
                {"kind": "api"},
            ]
        }
    )
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )

    assert required_release_service_kinds(project=project, preview_release=release) == ("website", "api")


def test_android_builtin_capture_runtime_requires_ready_adb_device(monkeypatch) -> None:
    target = DemoCaptureTarget(
        capture_target="android",
        capture_reference="android-emulator://configured",
        recorder_command=(sys.executable, "scripts/qa_demo_android_recorder.py"),
        required_worker_platform="linux",
    )
    monkeypatch.setattr("orchestrator.core.qa.demo_service.shutil.which", lambda command: f"/usr/bin/{command}")

    def _fake_run(args, **_kwargs):  # noqa: ANN001
        assert args == ["adb", "devices"]

        class _Result:
            stdout = "List of devices attached\nemulator-5554\toffline\n"

        return _Result()

    monkeypatch.setattr("orchestrator.core.qa.demo_service.subprocess.run", _fake_run)

    try:
        ensure_capture_target_runtime_ready(target)
    except RuntimeError as exc:
        assert "No available Android emulator/device" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected Android capture runtime readiness failure")


def test_android_builtin_capture_runtime_requires_recorder_toolchain_before_adb_probe(monkeypatch) -> None:
    target = DemoCaptureTarget(
        capture_target="android",
        capture_reference="android-emulator://configured",
        recorder_command=(sys.executable, "scripts/qa_demo_android_recorder.py"),
        required_worker_platform="linux",
    )

    def _which(command: str) -> str | None:
        if command == "aapt":
            return None
        return f"/usr/bin/{command}"

    adb_probe = MagicMock(side_effect=AssertionError("adb devices should not run when recorder tools are missing"))
    monkeypatch.setattr("orchestrator.core.qa.demo_service.shutil.which", _which)
    monkeypatch.setattr("orchestrator.core.qa.demo_service.subprocess.run", adb_probe)

    try:
        ensure_capture_target_runtime_ready(target)
    except RuntimeError as exc:
        assert "Android QA demo recording requires aapt on the worker PATH" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected Android recorder toolchain readiness failure")

    adb_probe.assert_not_called()


def test_upsert_demo_evidence_section_replaces_existing_section() -> None:
    body = "## Summary\n- change\n\n## Demo Evidence\n- old\n\n## How To Test\n- pytest -q"
    updated = upsert_demo_evidence_section(
        body=body,
        recordings=[
            QaRecording(
                name="Happy path",
                artifact_url="https://demo.example/happy.webm",
                object_key="qa/happy.webm",
                capture_reference="https://preview.example",
                content_sha256=_sha256(7),
                release_context_sha256=_release_context_sha256(),
            )
        ],
    )
    assert DEMO_EVIDENCE_HEADING in updated
    assert DEMO_EVIDENCE_MARKER in updated
    assert (
        "[target=browser; reference=https://preview.example; object_key=qa/happy.webm; sha256=" in updated
    )
    assert "release_context_sha256=" in updated
    assert "old" not in updated
    assert "https://demo.example/happy.webm" in updated
    assert "## How To Test" in updated


def test_upsert_demo_evidence_section_includes_required_capture_targets() -> None:
    updated = upsert_demo_evidence_section(
        body="## Summary\n- change",
        recordings=[
            QaRecording(
                name="Browser walkthrough",
                artifact_url="https://demo.example/browser.webm",
                object_key="qa/browser.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(8),
                release_context_sha256=_release_context_sha256(),
            ),
            QaRecording(
                name="iOS walkthrough",
                artifact_url="https://demo.example/ios.mp4",
                object_key="qa/ios.mp4",
                capture_target="ios",
                capture_reference="ios-simulator://configured",
                content_sha256=_sha256(9),
                release_context_sha256=_release_context_sha256(),
            ),
        ],
        required_capture_targets=("browser", "ios", "android"),
    )

    assert f"{DEMO_EVIDENCE_REQUIRED_TARGETS_MARKER} browser,ios,android -->" in updated


def test_upsert_demo_evidence_section_includes_required_recording_counts() -> None:
    updated = upsert_demo_evidence_section(
        body="## Summary\n- change",
        recordings=[
            QaRecording(
                name="Browser walkthrough",
                artifact_url="https://demo.example/browser.webm",
                object_key="qa/browser.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(10),
                release_context_sha256=_release_context_sha256(),
            ),
        ],
        required_recording_counts={"browser": 2, "ios": 1},
    )

    assert f"{DEMO_EVIDENCE_REQUIRED_COUNTS_MARKER} browser=2,ios=1 -->" in updated


def test_upsert_demo_evidence_section_rejects_multiline_recording_metadata() -> None:
    try:
        upsert_demo_evidence_section(
            body="## Summary\n- change",
            recordings=[
                QaRecording(
                    name="Browser walkthrough\n- Injected [target=ios; reference=ios-simulator://configured; "
                    "object_key=tenant-1/project-1/run-1/qa-demo-2.mp4]: "
                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-2.mp4",
                    artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm",
                    object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
                    capture_target="browser",
                    capture_reference="https://preview.example",
                    content_sha256=_sha256(11),
                    release_context_sha256=_release_context_sha256(),
                )
            ],
            required_capture_targets=("browser",),
            required_recording_counts={"browser": 1},
        )
    except RuntimeError as exc:
        assert "QA demo evidence recording metadata is not serializable" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected multiline recording metadata to block PR evidence rendering")


def test_upsert_demo_evidence_section_rejects_structural_delimiters_in_recording_metadata() -> None:
    try:
        upsert_demo_evidence_section(
            body="## Summary\n- change",
            recordings=[
                QaRecording(
                    name="Browser walkthrough",
                    artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm",
                    object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
                    capture_target="browser",
                    capture_reference="https://preview.example] [target=ios",
                    content_sha256=_sha256(12),
                    release_context_sha256=_release_context_sha256(),
                )
            ],
            required_capture_targets=("browser",),
            required_recording_counts={"browser": 1},
        )
    except RuntimeError as exc:
        assert "QA demo evidence recording metadata is not serializable" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected structured metadata delimiters to block PR evidence rendering")


def test_upsert_demo_evidence_section_rejects_unsupported_recording_capture_target() -> None:
    try:
        upsert_demo_evidence_section(
            body="## Summary\n- change",
            recordings=[
                QaRecording(
                    name="Tablet walkthrough",
                    artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm",
                    object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
                    capture_target="tablet",
                    capture_reference="tablet://configured",
                    content_sha256=_sha256(13),
                    release_context_sha256=_release_context_sha256(),
                )
            ],
            required_capture_targets=("tablet",),
            required_recording_counts={"tablet": 1},
        )
    except ValueError as exc:
        assert "Unsupported QA demo capture target" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected unsupported capture target to block PR evidence rendering")


def test_record_demo_scenarios_passes_explicit_playwright_module_dir() -> None:
    commands: list[tuple[list[str], dict[str, str], float | None]] = []

    def _run(cmd, **kwargs):  # noqa: ANN001
        commands.append((list(cmd), dict(kwargs.get("env") or {}), kwargs.get("timeout")))
        if cmd[0] != "node":
            raise AssertionError(f"unexpected command: {cmd}")
        output_path = Path(cmd[3])
        input_payload = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
        video_dir = Path(input_payload["output_dir"])
        video_dir.mkdir(parents=True, exist_ok=True)
        source_path = video_dir / "happy-path.webm"
        source_path.write_bytes(_fake_webm_payload())
        output_path.write_text(
            json.dumps({"recordings": [{"name": "Happy path", "path": str(source_path)}]}),
            encoding="utf-8",
        )
        return SimpleNamespace(stdout="")

    with patch("orchestrator.core.qa.demo_service.subprocess.run", side_effect=_run):
        recordings = record_demo_scenarios(
            settings=SimpleNamespace(
                qa_demo_playwright_module_dir="/tmp/playwright-modules",
                qa_demo_recorder_process_timeout_seconds=42,
            ),
            request=_request(),
            available_capture_targets=_browser_capture_targets(),
            qa_result=QaResult(
                summary=["Recorded demos"],
                scenarios=[
                    QaScenario(
                        name="Happy path",
                        objective="Show feature works",
                        steps=[QaStep(action="assert_visible", selector="#feature")],
                    )
                ],
            ),
        )

    assert len(recordings) == 1
    assert Path(recordings[0].path).exists()
    assert commands[0][1]["QA_DEMO_PLAYWRIGHT_MODULE_DIR"] == "/tmp/playwright-modules"
    assert commands[0][2] == 42.0


def test_record_demo_scenarios_scopes_copied_recordings_to_run_identity() -> None:
    def _run(cmd, **_kwargs):  # noqa: ANN001
        output_path = Path(cmd[3])
        input_payload = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
        video_dir = Path(input_payload["output_dir"])
        video_dir.mkdir(parents=True, exist_ok=True)
        source_path = video_dir / "happy-path.webm"
        source_path.write_bytes(_fake_webm_payload())
        output_path.write_text(
            json.dumps({"recordings": [{"name": "Happy path", "path": str(source_path)}]}),
            encoding="utf-8",
        )
        return SimpleNamespace(stdout="")

    with patch("orchestrator.core.qa.demo_service.subprocess.run", side_effect=_run):
        first_recordings = record_demo_scenarios(
            settings=SimpleNamespace(qa_demo_playwright_module_dir="/tmp/playwright-modules"),
            request=replace(_request(), run_id="run-1"),
            available_capture_targets=_browser_capture_targets(),
            qa_result=QaResult(
                summary=["Recorded demos"],
                scenarios=[
                    QaScenario(
                        name="Happy path",
                        objective="Show feature works",
                        steps=[QaStep(action="assert_visible", selector="#feature")],
                    )
                ],
            ),
        )
        second_recordings = record_demo_scenarios(
            settings=SimpleNamespace(qa_demo_playwright_module_dir="/tmp/playwright-modules"),
            request=replace(_request(), run_id="run-2"),
            available_capture_targets=_browser_capture_targets(),
            qa_result=QaResult(
                summary=["Recorded demos"],
                scenarios=[
                    QaScenario(
                        name="Happy path",
                        objective="Show feature works",
                        steps=[QaStep(action="assert_visible", selector="#feature")],
                    )
                ],
            ),
        )

    first_path = Path(first_recordings[0].path)
    second_path = Path(second_recordings[0].path)
    assert first_path != second_path
    assert "run-1" in first_path.parts
    assert "run-2" in second_path.parts
    assert first_path.exists()
    assert second_path.exists()


def test_record_demo_scenarios_rejects_browser_start_path_outside_preview_origin() -> None:
    with patch("orchestrator.core.qa.demo_service.subprocess.run") as run_mock:
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(qa_demo_playwright_module_dir="/tmp/playwright-modules"),
                request=_request(),
                available_capture_targets=_browser_capture_targets(),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="External start",
                            objective="Prove feature on another site",
                            start_path="https://evil.example/fake-feature",
                            steps=[QaStep(action="assert_visible", selector="text=Feature")],
                        )
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "QA demo browser scenario must stay on preview release origin" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected off-origin browser start path to block before recording")

    run_mock.assert_not_called()


def test_record_demo_scenarios_rejects_browser_goto_outside_preview_origin() -> None:
    with patch("orchestrator.core.qa.demo_service.subprocess.run") as run_mock:
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(qa_demo_playwright_module_dir="/tmp/playwright-modules"),
                request=_request(),
                available_capture_targets=_browser_capture_targets(),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="External redirect",
                            objective="Navigate away from release",
                            steps=[
                                QaStep(action="goto", value="//evil.example/fake-feature"),
                                QaStep(action="assert_visible", selector="text=Feature"),
                            ],
                        )
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "QA demo browser scenario must stay on preview release origin" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected off-origin browser navigation to block before recording")

    run_mock.assert_not_called()


def test_record_demo_scenarios_fails_when_recorder_process_times_out() -> None:
    with patch(
        "orchestrator.core.qa.demo_service.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["node", "/tmp/qa_demo_recorder.mjs"], timeout=2),
    ):
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(
                    qa_demo_playwright_module_dir="/tmp/playwright-modules",
                    qa_demo_recorder_process_timeout_seconds=2,
                ),
                request=_request(),
                available_capture_targets=_browser_capture_targets(),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Happy path",
                            objective="Show feature works",
                            steps=[QaStep(action="assert_visible", selector="#feature")],
                        )
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "QA demo recorder command timed out after 2.0 seconds" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected recorder timeout to fail QA demo recording")


def test_record_demo_scenarios_fails_when_playwright_module_lookup_times_out() -> None:
    with patch(
        "orchestrator.core.qa.demo_service.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["npm", "root", "-g"], timeout=3),
    ):
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(qa_demo_recorder_process_timeout_seconds=3),
                request=_request(),
                available_capture_targets=_browser_capture_targets(),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Happy path",
                            objective="Show feature works",
                            steps=[QaStep(action="assert_visible", selector="#feature")],
                        )
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "QA demo Playwright module lookup timed out after 3.0 seconds" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected Playwright module lookup timeout to fail QA demo recording")


def test_record_demo_scenarios_uses_configured_desktop_recorder() -> None:
    commands: list[tuple[list[str], dict[str, str], dict[str, object]]] = []

    def _run(cmd, **kwargs):  # noqa: ANN001
        env = dict(kwargs.get("env") or {})
        input_path = Path(cmd[-2])
        output_path = Path(cmd[-1])
        input_payload = json.loads(input_path.read_text(encoding="utf-8"))
        commands.append((list(cmd), env, input_payload))
        video_dir = Path(input_payload["output_dir"])
        video_dir.mkdir(parents=True, exist_ok=True)
        source_path = video_dir / "desktop-flow.mp4"
        source_path.write_bytes(_fake_mp4_payload())
        output_path.write_text(
            json.dumps({"recordings": [{"name": "Desktop flow", "path": str(source_path)}]}),
            encoding="utf-8",
        )
        return SimpleNamespace(stdout="")

    with patch("orchestrator.core.qa.demo_service.subprocess.run", side_effect=_run):
        recordings = record_demo_scenarios(
            settings=SimpleNamespace(),
            request=_request(),
            available_capture_targets={
                "desktop": DemoCaptureTarget(
                    capture_target="desktop",
                    capture_reference="desktop://macos-app",
                    recorder_command=("python", "/tmp/desktop_recorder.py"),
                )
            },
            qa_result=QaResult(
                summary=["Recorded demos"],
                scenarios=[
                    QaScenario(
                        name="Desktop flow",
                        objective="Show desktop app works",
                        capture_target="desktop",
                        steps=[QaStep(action="assert_visible", selector="text=Ready")],
                    )
                ],
            ),
        )

    assert len(recordings) == 1
    assert Path(recordings[0].path).exists()
    assert commands[0][0][:2] == ["python", "/tmp/desktop_recorder.py"]
    assert commands[0][2]["capture_target"] == "desktop"
    assert commands[0][2]["capture_reference"] == "desktop://macos-app"


def test_record_demo_scenarios_passes_project_source_paths_and_release_context_to_native_recorder() -> None:
    commands: list[tuple[dict[str, object], dict[str, str]]] = []

    def _run(cmd, **kwargs):  # noqa: ANN001
        input_path = Path(cmd[-2])
        output_path = Path(cmd[-1])
        input_payload = json.loads(input_path.read_text(encoding="utf-8"))
        commands.append((input_payload, dict(kwargs.get("env") or {})))
        video_dir = Path(input_payload["output_dir"])
        video_dir.mkdir(parents=True, exist_ok=True)
        source_path = video_dir / "android-flow.mp4"
        source_path.write_bytes(_fake_mp4_payload())
        output_path.write_text(
            json.dumps({"recordings": [{"name": "Android flow", "path": str(source_path)}]}),
            encoding="utf-8",
        )
        return SimpleNamespace(stdout="")

    request = replace(_request(), project_demo_capture_target_sources={"android": ("apps/android",)})

    with patch("orchestrator.core.qa.demo_service.subprocess.run", side_effect=_run):
        recordings = record_demo_scenarios(
            settings=SimpleNamespace(),
            request=request,
            available_capture_targets={
                "android": DemoCaptureTarget(
                    capture_target="android",
                    capture_reference="android-emulator://configured",
                    recorder_command=("python", "/tmp/android_recorder.py"),
                )
            },
            release_service_urls=[
                {
                    "service_kind": "api",
                    "service_name": "api",
                    "url": "https://api.preview.example",
                },
                {
                    "service_kind": "website",
                    "service_name": "web",
                    "url": "https://preview.example",
                },
            ],
            release_commit_sha="b" * 40,
            qa_result=QaResult(
                summary=["Recorded demos"],
                scenarios=[
                    QaScenario(
                        name="Android flow",
                        objective="Show Android app works",
                        capture_target="android",
                        steps=[QaStep(action="assert_visible", selector="text=Ready")],
                    )
                ],
            ),
        )

    assert len(recordings) == 1
    input_payload, env = commands[0]
    assert input_payload["target_source_paths"] == ["apps/android"]
    assert input_payload["release_commit_sha"] == "b" * 40
    assert input_payload["release_api_base_url"] == "https://api.preview.example"
    assert input_payload["release_browser_url"] == "https://preview.example"
    assert input_payload["release_service_urls"] == [
        {
            "service_kind": "api",
            "service_name": "api",
            "url": "https://api.preview.example",
        },
        {
            "service_kind": "website",
            "service_name": "web",
            "url": "https://preview.example",
        },
    ]
    assert env["MB_QA_DEMO_RELEASE_COMMIT_SHA"] == "b" * 40
    assert env["MB_QA_DEMO_RELEASE_API_BASE_URL"] == "https://api.preview.example"
    assert env["QA_DEMO_API_BASE_URL"] == "https://api.preview.example"
    assert json.loads(env["MB_QA_DEMO_RELEASE_SERVICE_URLS_JSON"])[0]["url"] == "https://api.preview.example"


def test_record_demo_scenarios_rejects_invalid_video_artifact() -> None:
    def _run(cmd, **_kwargs):  # noqa: ANN001
        output_path = Path(cmd[3])
        input_payload = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
        video_dir = Path(input_payload["output_dir"])
        video_dir.mkdir(parents=True, exist_ok=True)
        source_path = video_dir / "not-video.webm"
        source_path.write_bytes(b"not-a-real-video" * 128)
        output_path.write_text(
            json.dumps({"recordings": [{"name": "Invalid", "path": str(source_path)}]}),
            encoding="utf-8",
        )
        return SimpleNamespace(stdout="")

    with patch("orchestrator.core.qa.demo_service.subprocess.run", side_effect=_run):
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(qa_demo_playwright_module_dir="/tmp/playwright-modules"),
                request=_request(),
                available_capture_targets=_browser_capture_targets(),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Invalid",
                            objective="Reject invalid recording bytes",
                            steps=[QaStep(action="assert_visible", selector="#feature")],
                        )
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "not valid video evidence" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected invalid recording artifact rejection")


def test_record_demo_scenarios_rejects_duplicate_local_recording_paths() -> None:
    def _run(cmd, **_kwargs):  # noqa: ANN001
        output_path = Path(cmd[3])
        input_payload = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
        video_dir = Path(input_payload["output_dir"])
        video_dir.mkdir(parents=True, exist_ok=True)
        source_path = video_dir / "shared-proof.webm"
        source_path.write_bytes(_fake_webm_payload())
        output_path.write_text(
            json.dumps(
                {
                    "recordings": [
                        {"name": "Happy path", "path": str(source_path)},
                        {"name": "Bad input shows validation", "path": str(source_path)},
                    ]
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(stdout="")

    with patch("orchestrator.core.qa.demo_service.subprocess.run", side_effect=_run):
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(qa_demo_playwright_module_dir="/tmp/playwright-modules"),
                request=_request(),
                available_capture_targets=_browser_capture_targets(),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Happy path",
                            objective="Show feature works",
                            steps=[QaStep(action="assert_visible", selector="#feature")],
                        ),
                        QaScenario(
                            name="Bad input shows validation",
                            objective="Show validation works",
                            steps=[QaStep(action="assert_visible", selector="#validation")],
                        ),
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "QA demo recorder returned duplicate local recording path" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected duplicate local recording path to block")


def test_record_demo_scenarios_rejects_duplicate_recording_content_before_upload() -> None:
    def _run(cmd, **_kwargs):  # noqa: ANN001
        output_path = Path(cmd[3])
        input_payload = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
        video_dir = Path(input_payload["output_dir"])
        video_dir.mkdir(parents=True, exist_ok=True)
        first_path = video_dir / "happy-path.webm"
        second_path = video_dir / "bad-input.webm"
        duplicate_payload = _fake_webm_payload()
        first_path.write_bytes(duplicate_payload)
        second_path.write_bytes(duplicate_payload)
        output_path.write_text(
            json.dumps(
                {
                    "recordings": [
                        {"name": "Happy path", "path": str(first_path)},
                        {"name": "Bad input shows validation", "path": str(second_path)},
                    ]
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(stdout="")

    with patch("orchestrator.core.qa.demo_service.subprocess.run", side_effect=_run):
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(qa_demo_playwright_module_dir="/tmp/playwright-modules"),
                request=_request(),
                available_capture_targets=_browser_capture_targets(),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Happy path",
                            objective="Show feature works",
                            steps=[QaStep(action="assert_visible", selector="#feature")],
                        ),
                        QaScenario(
                            name="Bad input shows validation",
                            objective="Show validation works",
                            steps=[QaStep(action="assert_visible", selector="#validation")],
                        ),
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "QA demo recorder produced duplicate recording content" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected duplicate recording content to block")


def test_record_demo_scenarios_rejects_recording_path_outside_recorder_output_dir() -> None:
    def _run(cmd, **_kwargs):  # noqa: ANN001
        output_path = Path(cmd[3])
        input_payload = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
        video_dir = Path(input_payload["output_dir"])
        video_dir.mkdir(parents=True, exist_ok=True)
        external_path = video_dir.parent / "preexisting-proof.webm"
        external_path.write_bytes(_fake_webm_payload())
        output_path.write_text(
            json.dumps({"recordings": [{"name": "Happy path", "path": str(external_path)}]}),
            encoding="utf-8",
        )
        return SimpleNamespace(stdout="")

    with patch("orchestrator.core.qa.demo_service.subprocess.run", side_effect=_run):
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(qa_demo_playwright_module_dir="/tmp/playwright-modules"),
                request=_request(),
                available_capture_targets=_browser_capture_targets(),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Happy path",
                            objective="Show feature works",
                            steps=[QaStep(action="assert_visible", selector="#feature")],
                        )
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "QA demo recorder returned recording path outside recorder output directory" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected recording path outside recorder output directory to block")


def test_record_demo_scenarios_rejects_recorder_capture_target_mismatch() -> None:
    def _run(cmd, **_kwargs):  # noqa: ANN001
        output_path = Path(cmd[3])
        input_payload = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
        video_dir = Path(input_payload["output_dir"])
        video_dir.mkdir(parents=True, exist_ok=True)
        source_path = video_dir / "happy-path.webm"
        source_path.write_bytes(_fake_webm_payload())
        output_path.write_text(
            json.dumps(
                {
                    "recordings": [
                        {
                            "name": "Happy path",
                            "path": str(source_path),
                            "capture_target": "ios",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(stdout="")

    with patch("orchestrator.core.qa.demo_service.subprocess.run", side_effect=_run):
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(qa_demo_playwright_module_dir="/tmp/playwright-modules"),
                request=_request(),
                available_capture_targets=_browser_capture_targets(),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Happy path",
                            objective="Show feature works",
                            steps=[QaStep(action="assert_visible", selector="#feature")],
                        )
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "QA demo recorder returned capture target outside planned target" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected recorder capture target mismatch to block")


def test_record_demo_scenarios_rejects_recorder_capture_reference_mismatch() -> None:
    def _run(cmd, **_kwargs):  # noqa: ANN001
        output_path = Path(cmd[3])
        input_payload = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
        video_dir = Path(input_payload["output_dir"])
        video_dir.mkdir(parents=True, exist_ok=True)
        source_path = video_dir / "happy-path.webm"
        source_path.write_bytes(_fake_webm_payload())
        output_path.write_text(
            json.dumps(
                {
                    "recordings": [
                        {
                            "name": "Happy path",
                            "path": str(source_path),
                            "capture_reference": "https://different-preview.example",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(stdout="")

    with patch("orchestrator.core.qa.demo_service.subprocess.run", side_effect=_run):
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(qa_demo_playwright_module_dir="/tmp/playwright-modules"),
                request=_request(),
                available_capture_targets=_browser_capture_targets(),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Happy path",
                            objective="Show feature works",
                            steps=[QaStep(action="assert_visible", selector="#feature")],
                        )
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "QA demo recorder returned capture reference outside planned target" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected recorder capture reference mismatch to block")


def test_record_demo_scenarios_rejects_missing_recording_for_planned_scenario() -> None:
    def _run(cmd, **_kwargs):  # noqa: ANN001
        output_path = Path(cmd[3])
        input_payload = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
        video_dir = Path(input_payload["output_dir"])
        video_dir.mkdir(parents=True, exist_ok=True)
        source_path = video_dir / "happy-path.webm"
        source_path.write_bytes(_fake_webm_payload())
        output_path.write_text(
            json.dumps({"recordings": [{"name": "Happy path", "path": str(source_path)}]}),
            encoding="utf-8",
        )
        return SimpleNamespace(stdout="")

    with patch("orchestrator.core.qa.demo_service.subprocess.run", side_effect=_run):
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(qa_demo_playwright_module_dir="/tmp/playwright-modules"),
                request=_request(),
                available_capture_targets=_browser_capture_targets(),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Happy path",
                            objective="Show feature works",
                            steps=[QaStep(action="assert_visible", selector="#feature")],
                        ),
                        QaScenario(
                            name="Bad input shows validation",
                            objective="Show validation works",
                            steps=[QaStep(action="assert_visible", selector="#validation")],
                        ),
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "did not produce recording(s) for browser scenario(s): Bad input shows validation" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected missing scenario recording to block")


def test_record_demo_scenarios_rejects_unplanned_recording_before_upload() -> None:
    def _run(cmd, **_kwargs):  # noqa: ANN001
        output_path = Path(cmd[3])
        input_payload = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
        video_dir = Path(input_payload["output_dir"])
        video_dir.mkdir(parents=True, exist_ok=True)
        planned_path = video_dir / "happy-path.webm"
        extra_path = video_dir / "unplanned.webm"
        planned_path.write_bytes(_fake_webm_payload())
        extra_path.write_bytes(_fake_webm_payload())
        output_path.write_text(
            json.dumps(
                {
                    "recordings": [
                        {"name": "Happy path", "path": str(planned_path)},
                        {"name": "Unplanned walkthrough", "path": str(extra_path)},
                    ]
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(stdout="")

    with patch("orchestrator.core.qa.demo_service.subprocess.run", side_effect=_run):
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(qa_demo_playwright_module_dir="/tmp/playwright-modules"),
                request=_request(),
                available_capture_targets=_browser_capture_targets(),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Happy path",
                            objective="Show feature works",
                            steps=[QaStep(action="assert_visible", selector="#feature")],
                        )
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "QA demo recorder produced unplanned recording(s) for browser scenario(s)" in str(exc)
            assert "Unplanned walkthrough" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected unplanned recorder output to block")


def test_record_demo_scenarios_rejects_duplicate_recording_name_before_upload() -> None:
    def _run(cmd, **_kwargs):  # noqa: ANN001
        output_path = Path(cmd[3])
        input_payload = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
        video_dir = Path(input_payload["output_dir"])
        video_dir.mkdir(parents=True, exist_ok=True)
        first_path = video_dir / "happy-path-1.webm"
        second_path = video_dir / "happy-path-2.webm"
        first_path.write_bytes(_fake_webm_payload())
        second_path.write_bytes(_fake_webm_payload())
        output_path.write_text(
            json.dumps(
                {
                    "recordings": [
                        {"name": "Happy path", "path": str(first_path)},
                        {"name": "Happy path", "path": str(second_path)},
                    ]
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(stdout="")

    with patch("orchestrator.core.qa.demo_service.subprocess.run", side_effect=_run):
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(qa_demo_playwright_module_dir="/tmp/playwright-modules"),
                request=_request(),
                available_capture_targets=_browser_capture_targets(),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Happy path",
                            objective="Show feature works",
                            steps=[QaStep(action="assert_visible", selector="#feature")],
                        )
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "QA demo recorder produced duplicate recording(s) for browser scenario(s)" in str(exc)
            assert "Happy path" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected duplicate recorder output to block")


def test_record_demo_scenarios_rejects_duplicate_scenario_names_before_recording() -> None:
    with patch("orchestrator.core.qa.demo_service.subprocess.run") as run_mock:
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(qa_demo_playwright_module_dir="/tmp/playwright-modules"),
                request=_request(),
                available_capture_targets=_browser_capture_targets(),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Happy path",
                            objective="Show feature works",
                            steps=[QaStep(action="assert_visible", selector="#feature")],
                        ),
                        QaScenario(
                            name="Happy path",
                            objective="Show repeat behavior",
                            steps=[QaStep(action="assert_visible", selector="#repeat")],
                        ),
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "QA demo scenarios for browser must have unique names: Happy path" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected duplicate scenario names to block")

    run_mock.assert_not_called()


def test_record_demo_scenarios_rejects_scenario_without_executable_steps() -> None:
    with patch("orchestrator.core.qa.demo_service.subprocess.run") as run_mock:
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(qa_demo_playwright_module_dir="/tmp/playwright-modules"),
                request=_request(),
                available_capture_targets=_browser_capture_targets(),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Passive page load",
                            objective="Only opens the page",
                            steps=[],
                        )
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "requires executable steps" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected no-step scenario to block before recording")

    run_mock.assert_not_called()


def test_record_demo_scenarios_rejects_scenario_without_proof_step() -> None:
    with patch("orchestrator.core.qa.demo_service.subprocess.run") as run_mock:
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(qa_demo_playwright_module_dir="/tmp/playwright-modules"),
                request=_request(),
                available_capture_targets=_browser_capture_targets(),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Click only",
                            objective="Clicks without proving an outcome",
                            steps=[
                                QaStep(action="goto", value="/"),
                                QaStep(action="click", selector="#feature"),
                            ],
                        )
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "requires at least one proof assertion step" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected no-proof scenario to block before recording")

    run_mock.assert_not_called()


def test_execute_qa_demo_stage_records_and_uploads() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[
            SimpleNamespace(service_kind="api", service_name="api", status="active", url="https://api.preview.example"),
            SimpleNamespace(service_kind="website", service_name="web", status="active", url="https://preview.example"),
        ]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[_demo_requirement()],
    )
    dev_result = DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8")
    test_result = TestResult(guidance=["pytest -q"])
    review_result = ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8")

    fake_agents = SimpleNamespace(qa=MagicMock(return_value=_qa_result()))

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch(
            "orchestrator.core.qa.demo_service.record_demo_scenarios",
            return_value=[
                _local_recording(name="Happy path"),
                _local_recording(name="Repeat action remains safe", path="/tmp/repeat.webm"),
            ],
        ) as record_mock,
        patch(
            "orchestrator.core.qa.demo_service.storage_config_from_settings",
            return_value=SimpleNamespace(
                endpoint="minio:9000",
                access_key="key",
                secret_key="secret",
                bucket="qa-demos",
                public_base_url="https://cdn.example/qa-demos",
                secure=False,
            ),
        ),
        patch(
            "orchestrator.core.qa.demo_service.upload_recording",
            return_value="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm",
        ) as upload_mock,
        patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=200),
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200, create=True),
    ):
        result = execute_qa_demo_stage(
            session=SimpleNamespace(),
            settings=SimpleNamespace(qa_demo_playwright_module_dir=""),
            tenant=tenant,
            project=project,
            run=run,
            request=_request(),
            plan=plan,
            dev_result=dev_result,
            test_result=test_result,
            review_result=review_result,
            preview_release=release,
        )

    assert result.recordings[0].artifact_url.endswith("qa-demo-1.webm")
    assert result.recordings[0].capture_target == "browser"
    assert result.recordings[0].capture_reference == "https://preview.example"
    assert result.recordings[0].release_context_sha256 == _release_context_sha256_with_commit(include_api=True)
    available_targets = json.loads(fake_agents.qa.call_args.kwargs["available_capture_targets_json"])
    browser_target = next(item for item in available_targets if item["capture_target"] == "browser")
    assert browser_target["capture_reference"] == "https://preview.example"
    assert browser_target["source_paths"] == []
    assert browser_target["release_commit_sha"] == "b" * 40
    assert browser_target["release_api_base_url"] == "https://api.preview.example"
    assert browser_target["release_browser_url"] == "https://preview.example"
    assert record_mock.call_args.kwargs["release_service_urls"] == [
        {
            "service_kind": "api",
            "service_name": "api",
            "url": "https://api.preview.example",
        },
        {
            "service_kind": "website",
            "service_name": "web",
            "url": "https://preview.example",
        },
    ]
    assert record_mock.call_args.kwargs["release_commit_sha"] == "b" * 40
    assert upload_mock.call_args.kwargs["content_type"] == "video/webm"
    assert upload_mock.call_args.kwargs["release_context_sha256"] == _release_context_sha256_with_commit(include_api=True)


def test_execute_qa_demo_stage_rejects_unsafe_artifact_scope_before_upload() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="../run-1")
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[_demo_requirement()],
    )
    fake_agents = SimpleNamespace(qa=MagicMock(return_value=_qa_result()))

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch(
            "orchestrator.core.qa.demo_service.record_demo_scenarios",
            return_value=[
                _local_recording(name="Happy path"),
                _local_recording(name="Repeat action remains safe", path="/tmp/repeat.webm"),
            ],
        ),
        patch(
            "orchestrator.core.qa.demo_service.storage_config_from_settings",
            return_value=SimpleNamespace(
                endpoint="minio:9000",
                access_key="key",
                secret_key="secret",
                bucket="qa-demos",
                public_base_url="https://cdn.example/qa-demos",
                secure=False,
            ),
        ),
        patch(
            "orchestrator.core.qa.demo_service.upload_recording",
            return_value="https://cdn.example/qa-demos/tenant-1/project-1/../run-1/qa-demo-1.webm",
        ) as upload_mock,
        patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=200),
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200),
    ):
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=SimpleNamespace(qa_demo_playwright_module_dir="", qa_demo_max_attempts=1),
                tenant=tenant,
                project=project,
                run=run,
                request=replace(_request(), run_id="../run-1"),
                plan=plan,
                dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
                test_result=TestResult(guidance=["pytest -q"]),
                review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
                preview_release=release,
            )
        except RuntimeError as exc:
            assert "QA demo artifact object key scope has unsafe run_id" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected unsafe run scope to block before upload")

    upload_mock.assert_not_called()


def test_execute_qa_demo_stage_includes_project_source_paths_in_qa_prompt_targets() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works on Android"],
        risks=[],
        demo_requirements=[
            _demo_requirement(
                title="Android walkthrough",
                acceptance_criterion="Feature works on Android",
                capture_target="android",
            )
        ],
    )
    request = replace(_request(), project_demo_capture_target_sources={"android": ("apps/android",)})
    fake_agents = SimpleNamespace(
        qa=MagicMock(
            return_value=QaResult(
                summary=["Recorded demos"],
                scenarios=[
                    QaScenario(
                        name="Android walkthrough",
                        objective="Show Android feature works",
                        capture_target="android",
                        steps=[QaStep(action="assert_visible", selector="text=Ready")],
                    ),
                    QaScenario(
                        name="Android repeat action",
                        objective="Repeat action remains safe on Android",
                        capture_target="android",
                        steps=[QaStep(action="assert_visible", selector="text=Ready")],
                    ),
                ],
            )
        )
    )

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch(
            "orchestrator.core.qa.demo_service.record_demo_scenarios",
            return_value=[
                _local_recording(
                    name="Android walkthrough",
                    path="/tmp/android.mp4",
                    capture_target="android",
                    capture_reference="android-emulator://configured",
                    content_type="video/mp4",
                ),
                _local_recording(
                    name="Android repeat action",
                    path="/tmp/android-repeat.mp4",
                    capture_target="android",
                    capture_reference="android-emulator://configured",
                    content_type="video/mp4",
                ),
            ],
        ),
        patch(
            "orchestrator.core.qa.demo_service.storage_config_from_settings",
            return_value=SimpleNamespace(
                endpoint="minio:9000",
                access_key="key",
                secret_key="secret",
                bucket="qa-demos",
                public_base_url="https://cdn.example/qa-demos",
                secure=False,
            ),
        ),
        patch(
            "orchestrator.core.qa.demo_service.upload_recording",
            return_value="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.mp4",
        ),
        patch("orchestrator.core.qa.demo_service.ensure_capture_target_runtime_ready"),
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200),
    ):
        execute_qa_demo_stage(
            session=SimpleNamespace(),
            settings=SimpleNamespace(qa_demo_playwright_module_dir=""),
            tenant=tenant,
            project=project,
            run=run,
            request=request,
            plan=plan,
            dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
            test_result=TestResult(guidance=["./gradlew test"]),
            review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
            preview_release=_preview_release(),
        )

    available_targets = json.loads(fake_agents.qa.call_args.kwargs["available_capture_targets_json"])
    assert available_targets == [
        {
            "capture_target": "android",
            "capture_reference": "android-emulator://configured",
            "source_paths": ["apps/android"],
            "release_commit_sha": "b" * 40,
            "release_service_urls": [],
            "release_api_base_url": "",
            "release_browser_url": "",
        }
    ]


def test_execute_qa_demo_stage_requires_pm_demo_variants_before_qa_agent() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[
            DemoRequirement(title="Feature walkthrough", acceptance_criterion="Feature works", capture_target="browser")
        ],
    )
    fake_agents = SimpleNamespace(qa=MagicMock())

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
    ):
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=SimpleNamespace(qa_demo_playwright_module_dir=""),
                tenant=tenant,
                project=project,
                run=run,
                request=_request(),
                plan=plan,
                dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
                test_result=TestResult(guidance=["pytest -q"]),
                review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
                preview_release=release,
            )
        except RuntimeError as exc:
            assert "requires PM demo requirement variants" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected QA demo PM variant contract failure")

    fake_agents.qa.assert_not_called()


def test_execute_qa_demo_stage_requires_pm_demo_targets_to_cover_project_targets() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[
            DemoRequirement(
                title="Browser walkthrough",
                acceptance_criterion="Feature works",
                capture_target="browser",
                variants=["Repeat action remains safe"],
            )
        ],
    )
    request = replace(_request(), project_demo_capture_targets=("browser", "ios", "android"))
    fake_agents = SimpleNamespace(qa=MagicMock())

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
    ):
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=SimpleNamespace(qa_demo_playwright_module_dir=""),
                tenant=tenant,
                project=project,
                run=run,
                request=request,
                plan=plan,
                dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
                test_result=TestResult(guidance=["pytest -q"]),
                review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
                preview_release=release,
            )
        except RuntimeError as exc:
            assert "missing required project demo capture target(s): android, ios" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected QA demo project capture target contract failure")

    fake_agents.qa.assert_not_called()


def test_execute_qa_demo_stage_requires_release_commit_before_recording() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    release = SimpleNamespace(
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[_demo_requirement()],
    )
    fake_agents = SimpleNamespace(qa=MagicMock())

    with (
        patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=200),
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
    ):
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=SimpleNamespace(qa_demo_playwright_module_dir=""),
                tenant=tenant,
                project=project,
                run=run,
                request=_request(),
                plan=plan,
                dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
                test_result=TestResult(guidance=["pytest -q"]),
                review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
                preview_release=release,
            )
        except RuntimeError as exc:
            assert "QA demo recording requires a preview release commit SHA" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected missing release commit to block QA demo recording")

    fake_agents.qa.assert_not_called()


def test_execute_qa_demo_stage_blocks_when_qa_scenarios_do_not_cover_pm_variants() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature handles happy path and bad input"],
        risks=[],
        demo_requirements=[
            DemoRequirement(
                title="Feature walkthrough",
                acceptance_criterion="Feature handles happy path and bad input",
                capture_target="browser",
                variants=["Bad input shows validation", "Repeat action remains safe"],
            )
        ],
    )
    dev_result = DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8")
    test_result = TestResult(guidance=["pytest -q"])
    review_result = ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8")
    fake_agents = SimpleNamespace(qa=MagicMock(return_value=_qa_result()))

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch("orchestrator.core.qa.demo_service.record_demo_scenarios") as record_mock,
        patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=200),
    ):
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=SimpleNamespace(qa_demo_playwright_module_dir=""),
                tenant=tenant,
                project=project,
                run=run,
                request=_request(),
                plan=plan,
                dev_result=dev_result,
                test_result=test_result,
                review_result=review_result,
                preview_release=release,
            )
        except RuntimeError as exc:
            assert "browser: expected at least 3, got 2" in str(exc)
            assert "browser: Bad input shows validation" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected QA scenario variant coverage failure")

    record_mock.assert_not_called()


def test_execute_qa_demo_stage_blocks_when_qa_scenarios_do_not_name_pm_variant_coverage() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature handles happy path and bad input"],
        risks=[],
        demo_requirements=[
            DemoRequirement(
                title="Feature walkthrough",
                acceptance_criterion="Feature handles happy path and bad input",
                capture_target="browser",
                variants=["Bad input shows validation", "Repeat action remains safe"],
            )
        ],
    )
    dev_result = DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8")
    test_result = TestResult(guidance=["pytest -q"])
    review_result = ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8")
    fake_agents = SimpleNamespace(
        qa=MagicMock(
            return_value=QaResult(
                summary=["Planned demos"],
                scenarios=[
                    QaScenario(
                        name="Happy path",
                        objective="Show feature works",
                        steps=[
                            QaStep(action="goto", value="/"),
                            QaStep(action="assert_visible", selector="text=Feature"),
                        ],
                    ),
                    QaScenario(
                        name="Validation path",
                        objective="Show invalid entry",
                        steps=[
                            QaStep(action="goto", value="/"),
                            QaStep(action="assert_visible", selector="text=Feature"),
                        ],
                    ),
                    QaScenario(
                        name="Repeat path",
                        objective="Show repeated interaction",
                        steps=[
                            QaStep(action="goto", value="/"),
                            QaStep(action="assert_visible", selector="text=Feature"),
                        ],
                    ),
                ],
            )
        )
    )

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch("orchestrator.core.qa.demo_service.record_demo_scenarios") as record_mock,
        patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=200),
    ):
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=SimpleNamespace(qa_demo_playwright_module_dir=""),
                tenant=tenant,
                project=project,
                run=run,
                request=_request(),
                plan=plan,
                dev_result=dev_result,
                test_result=test_result,
                review_result=review_result,
                preview_release=release,
            )
        except RuntimeError as exc:
            assert "missing variant coverage: browser: Bad input shows validation" in str(exc)
            assert "browser: Repeat action remains safe" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected QA scenario variant traceability failure")

    record_mock.assert_not_called()


def test_execute_qa_demo_stage_blocks_when_qa_scenarios_do_not_name_pm_acceptance_criterion() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Cart total recalculates after quantity changes"],
        risks=[],
        demo_requirements=[
            DemoRequirement(
                title="Checkout total updates",
                acceptance_criterion="Cart total recalculates after quantity changes",
                capture_target="browser",
                variants=["Repeat quantity change remains safe"],
            )
        ],
    )
    fake_agents = SimpleNamespace(
        qa=MagicMock(
            return_value=QaResult(
                summary=["Planned demos"],
                scenarios=[
                    QaScenario(
                        name="Generic happy path",
                        objective="Show the feature works",
                        capture_target="browser",
                        steps=[
                            QaStep(action="goto", value="/"),
                            QaStep(action="assert_visible", selector="text=Ready"),
                        ],
                    ),
                    QaScenario(
                        name="Repeat quantity change remains safe",
                        objective="Repeat quantity change remains safe",
                        capture_target="browser",
                        steps=[
                            QaStep(action="goto", value="/"),
                            QaStep(action="assert_visible", selector="text=Ready"),
                        ],
                    ),
                ],
            )
        )
    )

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch("orchestrator.core.qa.demo_service.record_demo_scenarios") as record_mock,
        patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=200),
    ):
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=SimpleNamespace(qa_demo_playwright_module_dir=""),
                tenant=tenant,
                project=project,
                run=run,
                request=_request(),
                plan=plan,
                dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
                test_result=TestResult(guidance=["pytest -q"]),
                review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
                preview_release=release,
            )
        except RuntimeError as exc:
            assert "missing requirement coverage: browser: Checkout total updates" in str(exc)
            assert "Cart total recalculates after quantity changes" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected QA scenario acceptance-criterion traceability failure")

    record_mock.assert_not_called()


def test_execute_qa_demo_stage_blocks_when_project_api_service_is_missing_from_release() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(
        project_id="project-1",
        github_repository="https://github.com/acme/repo",
        deployment_config={
            "services": [
                {"kind": "website"},
                {"kind": "api"},
            ]
        },
    )
    run = SimpleNamespace(run_id="run-1")
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[_demo_requirement()],
    )
    dev_result = DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8")
    test_result = TestResult(guidance=["pytest -q"])
    review_result = ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8")

    runtime_mock = MagicMock()
    qa_agent = MagicMock(return_value=_qa_result())
    fake_agents = SimpleNamespace(qa=qa_agent)

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime", runtime_mock),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=200),
    ):
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=SimpleNamespace(qa_demo_playwright_module_dir=""),
                tenant=tenant,
                project=project,
                run=run,
                request=_request(),
                plan=plan,
                dev_result=dev_result,
                test_result=test_result,
                review_result=review_result,
                preview_release=release,
            )
        except RuntimeError as exc:
            assert "not active: api" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected QA demo stage to block on missing API release service")

    runtime_mock.assert_not_called()
    qa_agent.assert_not_called()


def test_execute_qa_demo_stage_retries_when_uploaded_artifact_url_is_unreachable() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[_demo_requirement()],
    )
    fake_agents = SimpleNamespace(qa=lambda **_: _qa_result())
    probe_attempts = {"count": 0}

    def _probe(_url: str, *, timeout_seconds: float) -> int:
        probe_attempts["count"] += 1
        if probe_attempts["count"] < 2:
            raise RuntimeError("artifact CDN returned 404")
        return 200

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch(
            "orchestrator.core.qa.demo_service.record_demo_scenarios",
            return_value=[
                _local_recording(name="Happy path"),
                _local_recording(name="Repeat action remains safe", path="/tmp/repeat.webm"),
            ],
        ),
        patch(
            "orchestrator.core.qa.demo_service.storage_config_from_settings",
            return_value=SimpleNamespace(
                endpoint="minio:9000",
                access_key="key",
                secret_key="secret",
                bucket="qa-demos",
                public_base_url="https://cdn.example/qa-demos",
                secure=False,
            ),
        ),
        patch(
            "orchestrator.core.qa.demo_service.upload_recording",
            return_value="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm",
        ),
        patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=200),
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", side_effect=_probe, create=True),
    ):
        result = execute_qa_demo_stage(
            session=SimpleNamespace(),
            settings=SimpleNamespace(qa_demo_playwright_module_dir="", qa_demo_max_attempts=2),
            tenant=tenant,
            project=project,
            run=run,
            request=_request(),
            plan=plan,
            dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
            test_result=TestResult(guidance=["pytest -q"]),
            review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
            preview_release=release,
        )

    assert probe_attempts["count"] == 3
    assert result.recordings[0].artifact_url.endswith("qa-demo-1.webm")


def test_execute_qa_demo_stage_normalizes_native_selectors_before_recording() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[
            _demo_requirement(
                title="Native walkthrough",
                acceptance_criterion="Feature works",
                capture_target="ios",
            )
        ],
    )
    dev_result = DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8")
    test_result = TestResult(guidance=["pytest -q"])
    review_result = ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8")
    fake_agents = SimpleNamespace(
        qa=MagicMock(
            return_value=QaResult(
                summary=["Recorded demos"],
                scenarios=[
                    QaScenario(
                        name="Mobile flow",
                        objective="Show feature works",
                        capture_target="ios",
                        steps=[
                            QaStep(action="assert_visible", selector="Start Free Demo"),
                            QaStep(action="click", selector="onboarding_primary_button"),
                        ],
                    ),
                    QaScenario(
                        name="Repeat action remains safe",
                        objective="Repeat action remains safe",
                        capture_target="ios",
                        steps=[QaStep(action="assert_visible", selector="Start Free Demo")],
                    )
                ],
            )
        )
    )

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch(
            "orchestrator.core.qa.demo_service.storage_config_from_settings",
            return_value=SimpleNamespace(
                endpoint="minio:9000",
                access_key="key",
                secret_key="secret",
                bucket="qa-demos",
                public_base_url="https://cdn.example/qa-demos",
                secure=False,
            ),
        ),
        patch(
            "orchestrator.core.qa.demo_service.record_demo_scenarios",
            return_value=[
                _local_recording(
                    name="Mobile flow",
                    path="/tmp/mobile.mp4",
                    capture_target="ios",
                    capture_reference="ios-simulator://configured",
                    content_type="video/mp4",
                ),
                _local_recording(
                    name="Repeat action remains safe",
                    path="/tmp/mobile-repeat.mp4",
                    capture_target="ios",
                    capture_reference="ios-simulator://configured",
                    content_type="video/mp4",
                ),
            ],
        ) as record_mock,
        patch(
            "orchestrator.core.qa.demo_service.upload_recording",
            return_value="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.mp4",
        ),
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200),
        patch("orchestrator.core.qa.demo_service.ensure_capture_target_runtime_ready"),
    ):
        execute_qa_demo_stage(
            session=SimpleNamespace(),
            settings=SimpleNamespace(),
            tenant=tenant,
            project=project,
            run=run,
            request=replace(_request(), current_worker_capability=WorkerCapability.MACOS),
            plan=plan,
            dev_result=dev_result,
            test_result=test_result,
            review_result=review_result,
            preview_release=_preview_release(),
        )

    normalized_result = record_mock.call_args.kwargs["qa_result"]
    steps = normalized_result.scenarios[0].steps
    assert steps[0].selector == "text=Start Free Demo"
    assert steps[1].selector == "id=onboarding_primary_button"


def test_execute_qa_demo_stage_normalizes_native_wait_for_text_selector_into_value() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[
            _demo_requirement(
                title="Native walkthrough",
                acceptance_criterion="Feature works",
                capture_target="ios",
            )
        ],
    )
    fake_agents = SimpleNamespace(
        qa=MagicMock(
            return_value=QaResult(
                summary=["Recorded demos"],
                scenarios=[
                    QaScenario(
                        name="Mobile flow",
                        objective="Show feature works",
                        capture_target="ios",
                        steps=[QaStep(action="wait_for_text", selector="text=Next")],
                    ),
                    QaScenario(
                        name="Repeat action remains safe",
                        objective="Repeat action remains safe",
                        capture_target="ios",
                        steps=[QaStep(action="assert_visible", selector="text=Next")],
                    )
                ],
            )
        )
    )

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch(
            "orchestrator.core.qa.demo_service.storage_config_from_settings",
            return_value=SimpleNamespace(
                endpoint="minio:9000",
                access_key="key",
                secret_key="secret",
                bucket="qa-demos",
                public_base_url="https://cdn.example/qa-demos",
                secure=False,
            ),
        ),
        patch(
            "orchestrator.core.qa.demo_service.record_demo_scenarios",
            return_value=[
                _local_recording(
                    name="Mobile flow",
                    path="/tmp/mobile.mp4",
                    capture_target="ios",
                    capture_reference="ios-simulator://configured",
                    content_type="video/mp4",
                ),
                _local_recording(
                    name="Repeat action remains safe",
                    path="/tmp/mobile-repeat.mp4",
                    capture_target="ios",
                    capture_reference="ios-simulator://configured",
                    content_type="video/mp4",
                ),
            ],
        ) as record_mock,
        patch(
            "orchestrator.core.qa.demo_service.upload_recording",
            return_value="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.mp4",
        ),
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200),
        patch("orchestrator.core.qa.demo_service.ensure_capture_target_runtime_ready"),
    ):
        execute_qa_demo_stage(
            session=SimpleNamespace(),
            settings=SimpleNamespace(),
            tenant=tenant,
            project=project,
            run=run,
            request=replace(_request(), current_worker_capability=WorkerCapability.MACOS),
            plan=plan,
            dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
            test_result=TestResult(guidance=["pytest -q"]),
            review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
            preview_release=_preview_release(),
        )

    step = record_mock.call_args.kwargs["qa_result"].scenarios[0].steps[0]
    assert step.selector is None
    assert step.value == "Next"


def test_execute_qa_demo_stage_rejects_transient_native_splash_assertions() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[
            _demo_requirement(
                title="Native walkthrough",
                acceptance_criterion="Feature works",
                capture_target="ios",
            )
        ],
    )
    dev_result = DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8")
    test_result = TestResult(guidance=["pytest -q"])
    review_result = ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8")
    fake_agents = SimpleNamespace(
        qa=MagicMock(
            return_value=QaResult(
                summary=["Recorded demos"],
                scenarios=[
                    QaScenario(
                        name="Splash assertion",
                        objective="Incorrectly assert transient splash",
                        capture_target="ios",
                        steps=[QaStep(action="assert_visible", selector="splash_screen")],
                    )
                ],
            )
        )
    )

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch("orchestrator.core.qa.demo_service.ensure_capture_target_runtime_ready"),
    ):
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=SimpleNamespace(),
                tenant=tenant,
                project=project,
                run=run,
                request=replace(_request(), current_worker_capability=WorkerCapability.MACOS),
                plan=plan,
                dev_result=dev_result,
                test_result=test_result,
                review_result=review_result,
                preview_release=_preview_release(),
            )
        except RuntimeError as exc:
            assert "splash_screen" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected transient native splash assertion failure")


def test_execute_qa_demo_stage_requeues_when_remaining_target_requires_another_worker() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works on iOS"],
        risks=[],
        demo_requirements=[
            _demo_requirement(
                title="Native walkthrough",
                acceptance_criterion="Feature works on iOS",
                capture_target="ios",
            )
        ],
    )

    with patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=200):
        result = execute_qa_demo_stage(
            session=SimpleNamespace(),
            settings=SimpleNamespace(qa_demo_playwright_module_dir=""),
            tenant=tenant,
            project=project,
            run=run,
            request=_request(),
            plan=plan,
            dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
            test_result=TestResult(guidance=["pytest -q"]),
            review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
            preview_release=release,
        )

    assert result.outcome == "requeue"
    assert "remaining capture target(s): ios" in str(result.feedback)
    assert result.recordings == []


def test_execute_qa_demo_stage_records_current_worker_targets_then_requeues_for_remaining_target() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works everywhere"],
        risks=[],
        demo_requirements=[
            _demo_requirement(
                title="Browser walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="browser",
            ),
            _demo_requirement(
                title="iOS walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="ios",
            ),
            _demo_requirement(
                title="Android walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="android",
            ),
        ],
    )
    fake_agents = SimpleNamespace(
        qa=MagicMock(
            return_value=QaResult(
                summary=["Recorded current worker demos"],
                scenarios=[
                    QaScenario(
                        name="Browser walkthrough",
                        objective="Show browser",
                        capture_target="browser",
                        steps=_proof_steps("text=Feature"),
                    ),
                    QaScenario(
                        name="Browser repeat action",
                        objective="Repeat action remains safe in browser",
                        capture_target="browser",
                        steps=_proof_steps("text=Feature"),
                    ),
                    QaScenario(
                        name="Android walkthrough",
                        objective="Show Android",
                        capture_target="android",
                        steps=_proof_steps("text=Ready"),
                    ),
                    QaScenario(
                        name="Android repeat action",
                        objective="Repeat action remains safe on Android",
                        capture_target="android",
                        steps=_proof_steps("text=Ready"),
                    ),
                ],
            )
        )
    )

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch(
            "orchestrator.core.qa.demo_service.record_demo_scenarios",
            return_value=[
                _local_recording(
                    name="Browser walkthrough",
                    path="/tmp/browser.webm",
                    capture_target="browser",
                    capture_reference="https://preview.example",
                    content_type="video/webm",
                ),
                _local_recording(
                    name="Browser repeat action",
                    path="/tmp/browser-repeat.webm",
                    capture_target="browser",
                    capture_reference="https://preview.example",
                    content_type="video/webm",
                ),
                _local_recording(
                    name="Android walkthrough",
                    path="/tmp/android.mp4",
                    capture_target="android",
                    capture_reference="android-emulator://configured",
                    content_type="video/mp4",
                ),
                _local_recording(
                    name="Android repeat action",
                    path="/tmp/android-repeat.mp4",
                    capture_target="android",
                    capture_reference="android-emulator://configured",
                    content_type="video/mp4",
                ),
            ],
        ),
        patch(
            "orchestrator.core.qa.demo_service.storage_config_from_settings",
            return_value=SimpleNamespace(
                endpoint="minio:9000",
                access_key="key",
                secret_key="secret",
                bucket="qa-demos",
                public_base_url="https://cdn.example/qa-demos",
                secure=False,
            ),
        ),
        patch(
            "orchestrator.core.qa.demo_service.upload_recording",
            side_effect=[
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm",
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-2.webm",
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-3.mp4",
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-4.mp4",
            ],
        ),
        patch("orchestrator.core.qa.demo_service.ensure_capture_target_runtime_ready"),
        patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=200),
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200),
    ):
        result = execute_qa_demo_stage(
            session=SimpleNamespace(),
            settings=SimpleNamespace(qa_demo_playwright_module_dir=""),
            tenant=tenant,
            project=project,
            run=run,
            request=_request(),
            plan=plan,
            dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
            test_result=TestResult(guidance=["pytest -q"]),
            review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
            preview_release=release,
        )

    assert result.outcome == "requeue"
    assert [recording.capture_target for recording in result.recordings] == [
        "browser",
        "browser",
        "android",
        "android",
    ]
    assert "still requires capture target(s): ios" in str(result.feedback)
    available_targets = json.loads(fake_agents.qa.call_args.kwargs["available_capture_targets_json"])
    assert [item["capture_target"] for item in available_targets] == ["browser", "android"]


def test_execute_qa_demo_stage_blocks_when_current_worker_target_proof_is_missing() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works on browser and Android"],
        risks=[],
        demo_requirements=[
            _demo_requirement(
                title="Browser walkthrough",
                acceptance_criterion="Feature works on browser",
                capture_target="browser",
            ),
            _demo_requirement(
                title="Android walkthrough",
                acceptance_criterion="Feature works on Android",
                capture_target="android",
            ),
        ],
    )
    fake_agents = SimpleNamespace(
        qa=MagicMock(
            return_value=QaResult(
                summary=["Recorded browser only"],
                scenarios=[
                    QaScenario(
                        name="Browser walkthrough",
                        objective="Show browser",
                        capture_target="browser",
                        steps=_proof_steps("text=Feature"),
                    ),
                ],
            )
        )
    )

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch(
            "orchestrator.core.qa.demo_service.record_demo_scenarios",
            return_value=[
                SimpleNamespace(
                    name="Browser walkthrough",
                    path="/tmp/browser.webm",
                    capture_target="browser",
                    capture_reference="https://preview.example",
                    content_type="video/webm",
                )
            ],
        ) as record_mock,
        patch(
            "orchestrator.core.qa.demo_service.storage_config_from_settings",
            return_value=SimpleNamespace(
                endpoint="minio:9000",
                access_key="key",
                secret_key="secret",
                bucket="qa-demos",
                public_base_url="https://cdn.example/qa-demos",
                secure=False,
            ),
        ),
        patch(
            "orchestrator.core.qa.demo_service.upload_recording",
            return_value="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm",
        ),
        patch("orchestrator.core.qa.demo_service.ensure_capture_target_runtime_ready"),
        patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=200),
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200),
    ):
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=SimpleNamespace(qa_demo_playwright_module_dir="", qa_demo_max_attempts=2),
                tenant=tenant,
                project=project,
                run=run,
                request=_request(),
                plan=plan,
                dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
                test_result=TestResult(guidance=["pytest -q"]),
                review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
                preview_release=release,
            )
        except RuntimeError as exc:
            assert "insufficient scenario count:" in str(exc)
            assert "browser: expected at least 2, got 1" in str(exc)
            assert "android: expected at least 2, got 0" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected current-worker missing target proof to block")

    record_mock.assert_not_called()


def test_execute_qa_demo_stage_completes_remaining_target_with_previous_recordings() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works everywhere"],
        risks=[],
        demo_requirements=[
            _demo_requirement(
                title="Browser walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="browser",
            ),
            _demo_requirement(
                title="iOS walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="ios",
            ),
            _demo_requirement(
                title="Android walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="android",
            ),
        ],
    )
    previous_qa = QaResult(
        summary=["Recorded Linux demos"],
        scenarios=[
            QaScenario(
                name="Browser walkthrough",
                objective="Show browser",
                capture_target="browser",
                steps=_proof_steps("text=Feature"),
            ),
            QaScenario(
                name="Browser repeat action",
                objective="Repeat action remains safe in browser",
                capture_target="browser",
                steps=_proof_steps("text=Feature"),
            ),
            QaScenario(
                name="Android walkthrough",
                objective="Show Android",
                capture_target="android",
                steps=_proof_steps("text=Ready"),
            ),
            QaScenario(
                name="Android repeat action",
                objective="Repeat action remains safe on Android",
                capture_target="android",
                steps=_proof_steps("text=Ready"),
            ),
        ],
        recordings=[
            QaRecording(
                name="Browser walkthrough",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(20),
                release_context_sha256=_empty_release_context_sha256(),
            ),
            QaRecording(
                name="Browser repeat action",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-2.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-2.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(21),
                release_context_sha256=_empty_release_context_sha256(),
            ),
            QaRecording(
                name="Android walkthrough",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-3.mp4",
                object_key="tenant-1/project-1/run-1/qa-demo-3.mp4",
                capture_target="android",
                capture_reference="android-emulator://configured",
                content_sha256=_sha256(22),
                release_context_sha256=_empty_release_context_sha256(),
            ),
            QaRecording(
                name="Android repeat action",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-4.mp4",
                object_key="tenant-1/project-1/run-1/qa-demo-4.mp4",
                capture_target="android",
                capture_reference="android-emulator://configured",
                content_sha256=_sha256(23),
                release_context_sha256=_empty_release_context_sha256(),
            ),
        ],
        outcome="requeue",
    )
    fake_agents = SimpleNamespace(
        qa=MagicMock(
            return_value=QaResult(
                summary=["Recorded iOS demo"],
                scenarios=[
                    QaScenario(
                        name="iOS walkthrough",
                        objective="Show iOS",
                        capture_target="ios",
                        steps=_proof_steps("text=Ready"),
                    ),
                    QaScenario(
                        name="iOS repeat action",
                        objective="Repeat action remains safe on iOS",
                        capture_target="ios",
                        steps=_proof_steps("text=Ready"),
                    )
                ],
            )
        )
    )
    request = replace(_request(), current_worker_capability=WorkerCapability.MACOS)

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch(
            "orchestrator.core.qa.demo_service.record_demo_scenarios",
            return_value=[
                _local_recording(
                    name="iOS walkthrough",
                    path="/tmp/ios.mp4",
                    capture_target="ios",
                    capture_reference="ios-simulator://configured",
                    content_type="video/mp4",
                ),
                _local_recording(
                    name="iOS repeat action",
                    path="/tmp/ios-repeat.mp4",
                    capture_target="ios",
                    capture_reference="ios-simulator://configured",
                    content_type="video/mp4",
                ),
            ],
        ),
        patch(
            "orchestrator.core.qa.demo_service.storage_config_from_settings",
            return_value=SimpleNamespace(
                endpoint="minio:9000",
                access_key="key",
                secret_key="secret",
                bucket="qa-demos",
                public_base_url="https://cdn.example/qa-demos",
                secure=False,
            ),
        ),
        patch(
            "orchestrator.core.qa.demo_service.upload_recording",
            return_value="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-3.mp4",
        ) as upload_mock,
        patch("orchestrator.core.qa.demo_service.ensure_capture_target_runtime_ready"),
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200),
    ):
        result = execute_qa_demo_stage(
            session=SimpleNamespace(),
            settings=SimpleNamespace(qa_demo_playwright_module_dir=""),
            tenant=tenant,
            project=project,
            run=run,
            request=request,
            plan=plan,
            dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
            test_result=TestResult(guidance=["pytest -q"]),
            review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
            preview_release=_preview_release(),
            previous_qa_result=previous_qa,
        )

    assert result.outcome == "continue"
    assert [recording.capture_target for recording in result.recordings] == [
        "browser",
        "browser",
        "android",
        "android",
        "ios",
        "ios",
    ]
    assert upload_mock.call_args.kwargs["object_key"].endswith("qa-demo-6.mp4")
    available_targets = json.loads(fake_agents.qa.call_args.kwargs["available_capture_targets_json"])
    assert available_targets == [
        {
            "capture_target": "ios",
            "capture_reference": "ios-simulator://configured",
            "source_paths": [],
            "release_commit_sha": "b" * 40,
            "release_service_urls": [],
            "release_api_base_url": "",
            "release_browser_url": "",
        }
    ]


def test_execute_qa_demo_stage_revalidates_partial_previous_recording_links_before_reuse() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works everywhere"],
        risks=[],
        demo_requirements=[
            _demo_requirement(
                title="Browser walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="browser",
            ),
            _demo_requirement(
                title="iOS walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="ios",
            ),
            _demo_requirement(
                title="Android walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="android",
            ),
        ],
    )
    previous_qa = QaResult(
        summary=["Recorded Linux demos"],
        scenarios=[
            QaScenario(
                name="Browser walkthrough",
                objective="Show browser",
                capture_target="browser",
                steps=_proof_steps("text=Feature"),
            ),
            QaScenario(
                name="Browser repeat action",
                objective="Repeat action remains safe in browser",
                capture_target="browser",
                steps=_proof_steps("text=Feature"),
            ),
            QaScenario(
                name="Android walkthrough",
                objective="Show Android",
                capture_target="android",
                steps=_proof_steps("text=Ready"),
            ),
            QaScenario(
                name="Android repeat action",
                objective="Repeat action remains safe on Android",
                capture_target="android",
                steps=_proof_steps("text=Ready"),
            ),
        ],
        recordings=[
            QaRecording(
                name="Browser walkthrough",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(24),
                release_context_sha256=_empty_release_context_sha256(),
            ),
            QaRecording(
                name="Browser repeat action",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-2.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-2.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(25),
                release_context_sha256=_empty_release_context_sha256(),
            ),
            QaRecording(
                name="Android walkthrough",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-3.mp4",
                object_key="tenant-1/project-1/run-1/qa-demo-3.mp4",
                capture_target="android",
                capture_reference="android-emulator://configured",
                content_sha256=_sha256(26),
                release_context_sha256=_empty_release_context_sha256(),
            ),
            QaRecording(
                name="Android repeat action",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-4.mp4",
                object_key="tenant-1/project-1/run-1/qa-demo-4.mp4",
                capture_target="android",
                capture_reference="android-emulator://configured",
                content_sha256=_sha256(27),
                release_context_sha256=_empty_release_context_sha256(),
            ),
        ],
        outcome="requeue",
    )
    fake_agents = SimpleNamespace(
        qa=MagicMock(
            return_value=QaResult(
                summary=["Recorded iOS demo"],
                scenarios=[
                    QaScenario(
                        name="iOS walkthrough",
                        objective="Show iOS",
                        capture_target="ios",
                        steps=_proof_steps("text=Ready"),
                    ),
                    QaScenario(
                        name="iOS repeat action",
                        objective="Repeat action remains safe on iOS",
                        capture_target="ios",
                        steps=_proof_steps("text=Ready"),
                    ),
                ],
            )
        )
    )
    request = replace(_request(), current_worker_capability=WorkerCapability.MACOS)

    def _probe_artifact_url(url: str, **_kwargs: object) -> int:
        if "qa-demo-1.webm" in url:
            raise RuntimeError("object expired")
        return 200

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime") as runtime_mock,
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents) as agents_mock,
        patch(
            "orchestrator.core.qa.demo_service.record_demo_scenarios",
            return_value=[
                _local_recording(
                    name="iOS walkthrough",
                    path="/tmp/ios.mp4",
                    capture_target="ios",
                    capture_reference="ios-simulator://configured",
                    content_type="video/mp4",
                ),
                _local_recording(
                    name="iOS repeat action",
                    path="/tmp/ios-repeat.mp4",
                    capture_target="ios",
                    capture_reference="ios-simulator://configured",
                    content_type="video/mp4",
                ),
            ],
        ) as record_mock,
        patch(
            "orchestrator.core.qa.demo_service.upload_recording",
            side_effect=[
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-5.mp4",
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-6.mp4",
            ],
        ) as upload_mock,
        patch("orchestrator.core.qa.demo_service.ensure_capture_target_runtime_ready") as runtime_ready_mock,
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", side_effect=_probe_artifact_url),
    ):
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(qa_demo_playwright_module_dir=""),
                tenant=tenant,
                project=project,
                run=run,
                request=request,
                plan=plan,
                dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
                test_result=TestResult(guidance=["pytest -q"]),
                review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
                preview_release=_preview_release(),
                previous_qa_result=previous_qa,
            )
        except RuntimeError as exc:
            assert "QA demo artifact URL is not reachable" in str(exc)
            assert "object expired" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected stale partial previous QA demo artifact URL to block reuse")

    runtime_mock.assert_not_called()
    agents_mock.assert_not_called()
    fake_agents.qa.assert_not_called()
    runtime_ready_mock.assert_not_called()
    record_mock.assert_not_called()
    upload_mock.assert_not_called()


def test_execute_qa_demo_stage_revalidates_previous_recording_links_before_short_circuit() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[_demo_requirement()],
    )
    previous_qa = QaResult(
        summary=["Previous demos"],
        scenarios=[
            QaScenario(
                name="Happy path",
                objective="Show feature works",
                capture_target="browser",
                steps=_proof_steps("text=Feature"),
            ),
            QaScenario(
                name="Repeat action remains safe",
                objective="Repeat action remains safe",
                capture_target="browser",
                steps=_proof_steps("text=Feature"),
            ),
        ],
        recordings=[
            QaRecording(
                name="Happy path",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(28),
                release_context_sha256=_release_context_sha256(),
            ),
            QaRecording(
                name="Repeat action remains safe",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-2.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-2.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(29),
                release_context_sha256=_release_context_sha256(),
            ),
        ],
    )

    with (
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", side_effect=RuntimeError("object expired")),
        patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=200),
    ):
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(),
                tenant=tenant,
                project=project,
                run=run,
                request=_request(),
                plan=plan,
                dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
                test_result=TestResult(guidance=["pytest -q"]),
                review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
                preview_release=SimpleNamespace(
                    commit_sha="b" * 40,
                    service_urls=[
                        SimpleNamespace(service_kind="website", status="active", url="https://preview.example")
                    ]
                ),
                previous_qa_result=previous_qa,
            )
        except RuntimeError as exc:
            assert "QA demo artifact URL is not reachable" in str(exc)
            assert "object expired" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected stale previous QA demo artifact URL to block short-circuit")


def test_execute_qa_demo_stage_revalidates_release_readiness_before_previous_recording_short_circuit() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[_demo_requirement()],
    )
    previous_qa = QaResult(
        summary=["Previous demos"],
        scenarios=[
            QaScenario(
                name="Happy path",
                objective="Show feature works",
                capture_target="browser",
                steps=_proof_steps("text=Feature"),
            ),
            QaScenario(
                name="Repeat action remains safe",
                objective="Repeat action remains safe",
                capture_target="browser",
                steps=_proof_steps("text=Feature"),
            ),
        ],
        recordings=[
            QaRecording(
                name="Happy path",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(50),
                release_context_sha256=_release_context_sha256(),
            ),
            QaRecording(
                name="Repeat action remains safe",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-2.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-2.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(51),
                release_context_sha256=_release_context_sha256(),
            ),
        ],
    )

    with (
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200),
        patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=404),
    ):
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(),
                tenant=tenant,
                project=project,
                run=run,
                request=_request(),
                plan=plan,
                dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
                test_result=TestResult(guidance=["pytest -q"]),
                review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
                preview_release=SimpleNamespace(
                    service_urls=[
                        SimpleNamespace(service_kind="website", status="active", url="https://preview.example")
                    ]
                ),
                previous_qa_result=previous_qa,
            )
        except RuntimeError as exc:
            assert "QA demo recording requires reachable release service URL(s)" in str(exc)
            assert "website (https://preview.example): HTTP 404" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected broken preview release to block previous QA demo proof reuse")


def test_execute_qa_demo_stage_rejects_previous_recording_without_current_pm_coverage() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["New dashboard saves settings"],
        risks=[],
        demo_requirements=[
            DemoRequirement(
                title="New dashboard walkthrough",
                acceptance_criterion="New dashboard saves settings",
                capture_target="browser",
                variants=["Invalid settings show validation"],
            )
        ],
    )
    previous_qa = QaResult(
        summary=["Previous demos"],
        scenarios=[
            QaScenario(
                name="Old profile walkthrough",
                objective="Show old profile still renders",
                capture_target="browser",
                steps=_proof_steps("text=Old profile"),
            ),
            QaScenario(
                name="Old profile repeat action",
                objective="Repeat action remains safe on the old profile",
                capture_target="browser",
                steps=_proof_steps("text=Old profile"),
            ),
        ],
        recordings=[
            QaRecording(
                name="Old profile walkthrough",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(55),
                release_context_sha256=_release_context_sha256(),
            ),
            QaRecording(
                name="Old profile repeat action",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-2.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-2.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(56),
                release_context_sha256=_release_context_sha256(),
            ),
        ],
    )

    with (
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200) as artifact_probe,
        patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=200),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents") as agents_cls,
    ):
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(),
                tenant=tenant,
                project=project,
                run=run,
                request=_request(),
                plan=plan,
                dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
                test_result=TestResult(guidance=["pytest -q"]),
                review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
                preview_release=SimpleNamespace(
                    service_urls=[
                        SimpleNamespace(service_kind="website", status="active", url="https://preview.example")
                    ]
                ),
                previous_qa_result=previous_qa,
            )
        except RuntimeError as exc:
            assert "QA demo scenarios do not cover PM demo requirement variants" in str(exc)
            assert "New dashboard walkthrough" in str(exc)
            assert "Invalid settings show validation" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected stale PM coverage proof to block previous QA demo proof reuse")

    artifact_probe.assert_not_called()
    agents_cls.assert_not_called()


def test_execute_qa_demo_stage_rejects_previous_recording_from_different_release_context() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[_demo_requirement()],
    )
    previous_qa = QaResult(
        summary=["Previous demos"],
        scenarios=[
            QaScenario(
                name="Happy path",
                objective="Show feature works",
                capture_target="browser",
                steps=_proof_steps("text=Feature"),
            ),
            QaScenario(
                name="Repeat action remains safe",
                objective="Repeat action remains safe",
                capture_target="browser",
                steps=_proof_steps("text=Feature"),
            ),
        ],
        recordings=[
            QaRecording(
                name="Happy path",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(52),
                release_context_sha256="f" * 64,
            )
        ],
    )

    with (
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200) as artifact_probe,
        patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=200),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents") as agents_cls,
    ):
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(),
                tenant=tenant,
                project=project,
                run=run,
                request=_request(),
                plan=plan,
                dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
                test_result=TestResult(guidance=["pytest -q"]),
                review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
                preview_release=SimpleNamespace(
                    commit_sha="b" * 40,
                    service_urls=[
                        SimpleNamespace(service_kind="website", service_name="web", status="active", url="https://preview.example")
                    ]
                ),
                previous_qa_result=previous_qa,
            )
        except RuntimeError as exc:
            assert "release context does not match current release before reuse" in str(exc)
            assert "Happy path" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected stale release-context proof to block previous QA demo proof reuse")

    artifact_probe.assert_not_called()
    agents_cls.assert_not_called()


def test_execute_qa_demo_stage_rejects_previous_recording_from_different_release_commit() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[_demo_requirement()],
    )
    previous_qa = QaResult(
        summary=["Previous demos"],
        scenarios=[
            QaScenario(
                name="Happy path",
                objective="Show feature works",
                capture_target="browser",
                steps=_proof_steps("text=Feature"),
            ),
            QaScenario(
                name="Repeat action remains safe",
                objective="Repeat action remains safe",
                capture_target="browser",
                steps=_proof_steps("text=Feature"),
            ),
        ],
        recordings=[
            QaRecording(
                name="Happy path",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(53),
                release_context_sha256=_release_context_sha256_with_commit(commit_sha="a" * 40),
            ),
            QaRecording(
                name="Repeat action remains safe",
                artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-2.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-2.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(54),
                release_context_sha256=_release_context_sha256_with_commit(commit_sha="a" * 40),
            ),
        ],
    )

    with (
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200) as artifact_probe,
        patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=200),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents") as agents_cls,
    ):
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(),
                tenant=tenant,
                project=project,
                run=run,
                request=_request(),
                plan=plan,
                dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
                test_result=TestResult(guidance=["pytest -q"]),
                review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
                preview_release=SimpleNamespace(
                    commit_sha="b" * 40,
                    service_urls=[
                        SimpleNamespace(service_kind="website", status="active", url="https://preview.example")
                    ],
                ),
                previous_qa_result=previous_qa,
            )
        except RuntimeError as exc:
            assert "release context does not match current release before reuse" in str(exc)
            assert "Happy path" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected stale release-commit proof to block previous QA demo proof reuse")

    artifact_probe.assert_not_called()
    agents_cls.assert_not_called()


def test_execute_qa_demo_stage_rejects_previous_recording_without_matching_scenario() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works on browser"],
        risks=[],
        demo_requirements=[
            _demo_requirement(
                title="Browser walkthrough",
                acceptance_criterion="Feature works on browser",
                capture_target="browser",
            ),
        ],
    )
    previous_qa = QaResult(
        summary=["Recorded browser demo"],
        scenarios=[],
        recordings=[
            QaRecording(
                name="Browser walkthrough",
                artifact_url="https://cdn.example/qa-demo-1.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=_sha256(45),
                release_context_sha256=_release_context_sha256(),
            )
        ],
        outcome="requeue",
    )

    with patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents") as agents_cls:
        try:
            execute_qa_demo_stage(
                session=SimpleNamespace(),
                settings=SimpleNamespace(qa_demo_playwright_module_dir=""),
                tenant=tenant,
                project=project,
                run=run,
                request=_request(),
                plan=plan,
                dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
                test_result=TestResult(guidance=["pytest -q"]),
                review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
                preview_release=_preview_release(),
                previous_qa_result=previous_qa,
            )
        except RuntimeError as exc:
            assert "missing matching executable scenario(s): browser: Browser walkthrough" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected orphaned previous recording proof to block")

    agents_cls.assert_not_called()


def test_execute_qa_demo_stage_uses_builtin_ios_capture_on_macos() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works on iOS"],
        risks=[],
        demo_requirements=[
            _demo_requirement(
                title="Native walkthrough",
                acceptance_criterion="Feature works on iOS",
                capture_target="ios",
            )
        ],
    )
    dev_result = DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8")
    test_result = TestResult(guidance=["xcodebuild test"])
    review_result = ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8")
    request = replace(
        _request(),
        current_worker_capability=WorkerCapability.MACOS,
        available_worker_capabilities=(WorkerCapability.MACOS,),
    )
    fake_agents = SimpleNamespace(
        qa=MagicMock(
            return_value=QaResult(
                summary=["Recorded demos"],
                scenarios=[
                    QaScenario(
                        name="Native walkthrough",
                        objective="Show iOS feature works",
                        capture_target="ios",
                        steps=[QaStep(action="assert_visible", selector="text=Ready")],
                    ),
                    QaScenario(
                        name="Native repeat action",
                        objective="Repeat action remains safe on iOS",
                        capture_target="ios",
                        steps=[QaStep(action="assert_visible", selector="text=Ready")],
                    )
                ],
            )
        )
    )

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch(
            "orchestrator.core.qa.demo_service.record_demo_scenarios",
            return_value=[
                _local_recording(
                    name="Native walkthrough",
                    path="/tmp/native.mp4",
                    capture_target="ios",
                    capture_reference="ios-simulator://configured",
                    content_type="video/mp4",
                ),
                _local_recording(
                    name="Native repeat action",
                    path="/tmp/native-repeat.mp4",
                    capture_target="ios",
                    capture_reference="ios-simulator://configured",
                    content_type="video/mp4",
                ),
            ],
        ),
        patch(
            "orchestrator.core.qa.demo_service.storage_config_from_settings",
            return_value=SimpleNamespace(
                endpoint="minio:9000",
                access_key="key",
                secret_key="secret",
                bucket="qa-demos",
                public_base_url="https://cdn.example/qa-demos",
                secure=False,
            ),
        ),
        patch(
            "orchestrator.core.qa.demo_service.upload_recording",
            return_value="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.mp4",
        ),
        patch("orchestrator.core.qa.demo_service.ensure_capture_target_runtime_ready"),
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200),
    ):
        result = execute_qa_demo_stage(
            session=SimpleNamespace(),
            settings=SimpleNamespace(qa_demo_playwright_module_dir=""),
            tenant=tenant,
            project=project,
            run=run,
            request=request,
            plan=plan,
            dev_result=dev_result,
            test_result=test_result,
            review_result=review_result,
            preview_release=_preview_release(),
        )

    assert result.recordings[0].artifact_url.endswith("qa-demo-1.mp4")
    assert result.recordings[0].capture_target == "ios"
    available_targets = json.loads(fake_agents.qa.call_args.kwargs["available_capture_targets_json"])
    assert available_targets == [
        {
            "capture_target": "ios",
            "capture_reference": "ios-simulator://configured",
            "source_paths": [],
            "release_commit_sha": "b" * 40,
            "release_service_urls": [],
            "release_api_base_url": "",
            "release_browser_url": "",
        }
    ]


def test_execute_qa_demo_stage_uses_builtin_android_capture_on_linux() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works on Android"],
        risks=[],
        demo_requirements=[
            _demo_requirement(
                title="Android walkthrough",
                acceptance_criterion="Feature works on Android",
                capture_target="android",
            )
        ],
    )
    fake_agents = SimpleNamespace(
        qa=MagicMock(
            return_value=QaResult(
                summary=["Recorded demos"],
                scenarios=[
                    QaScenario(
                        name="Android walkthrough",
                        objective="Show Android feature works",
                        capture_target="android",
                        steps=[QaStep(action="assert_visible", selector="text=Ready")],
                    ),
                    QaScenario(
                        name="Android repeat action",
                        objective="Repeat action remains safe on Android",
                        capture_target="android",
                        steps=[QaStep(action="assert_visible", selector="text=Ready")],
                    )
                ],
            )
        )
    )

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch(
            "orchestrator.core.qa.demo_service.record_demo_scenarios",
            return_value=[
                _local_recording(
                    name="Android walkthrough",
                    path="/tmp/android.mp4",
                    capture_target="android",
                    capture_reference="android-emulator://configured",
                    content_type="video/mp4",
                ),
                _local_recording(
                    name="Android repeat action",
                    path="/tmp/android-repeat.mp4",
                    capture_target="android",
                    capture_reference="android-emulator://configured",
                    content_type="video/mp4",
                ),
            ],
        ),
        patch(
            "orchestrator.core.qa.demo_service.storage_config_from_settings",
            return_value=SimpleNamespace(
                endpoint="minio:9000",
                access_key="key",
                secret_key="secret",
                bucket="qa-demos",
                public_base_url="https://cdn.example/qa-demos",
                secure=False,
            ),
        ),
        patch(
            "orchestrator.core.qa.demo_service.upload_recording",
            return_value="https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.mp4",
        ),
        patch("orchestrator.core.qa.demo_service.ensure_capture_target_runtime_ready"),
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200),
    ):
        result = execute_qa_demo_stage(
            session=SimpleNamespace(),
            settings=SimpleNamespace(qa_demo_playwright_module_dir=""),
            tenant=tenant,
            project=project,
            run=run,
            request=_request(),
            plan=plan,
            dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
            test_result=TestResult(guidance=["./gradlew test"]),
            review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
            preview_release=_preview_release(),
        )

    assert result.recordings[0].capture_target == "android"
    available_targets = json.loads(fake_agents.qa.call_args.kwargs["available_capture_targets_json"])
    assert available_targets == [
        {
            "capture_target": "android",
            "capture_reference": "android-emulator://configured",
            "source_paths": [],
            "release_commit_sha": "b" * 40,
            "release_service_urls": [],
            "release_api_base_url": "",
            "release_browser_url": "",
        }
    ]


def test_execute_qa_demo_stage_requeues_when_desktop_capture_requires_macos_worker() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Desktop feature works"],
        risks=[],
        demo_requirements=[
            _demo_requirement(
                title="Desktop walkthrough",
                acceptance_criterion="Desktop feature works",
                capture_target="desktop",
            )
        ],
    )

    result = execute_qa_demo_stage(
        session=SimpleNamespace(),
        settings=SimpleNamespace(
            qa_demo_playwright_module_dir="",
            qa_demo_desktop_recorder_command="python /tmp/desktop_recorder.py",
            qa_demo_desktop_worker_platform="macos",
        ),
        tenant=tenant,
        project=project,
        run=run,
        request=_request(),
        plan=plan,
        dev_result=DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8"),
        test_result=TestResult(guidance=["pytest -q"]),
        review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
        preview_release=_preview_release(),
    )

    assert result.outcome == "requeue"
    assert "remaining capture target(s): desktop" in str(result.feedback)
    assert result.recordings == []


def test_record_demo_scenarios_surfaces_recorder_stderr() -> None:
    with patch(
        "orchestrator.core.qa.demo_service.subprocess.run",
        side_effect=subprocess.CalledProcessError(
            1,
            ["python", "/tmp/ios_recorder.py"],
            stderr="XCTAssertTrue failed - Missing visible element: splash_screen",
        ),
    ):
        try:
            record_demo_scenarios(
                settings=SimpleNamespace(),
                request=replace(_request(), current_worker_capability=WorkerCapability.MACOS),
                available_capture_targets={
                    "ios": DemoCaptureTarget(
                        capture_target="ios",
                        capture_reference="ios-simulator://configured",
                        recorder_command=("python", "/tmp/ios_recorder.py"),
                        required_worker_platform="macos",
                    )
                },
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Mobile flow",
                            objective="Show feature works",
                            capture_target="ios",
                            steps=[QaStep(action="assert_visible", selector="id=onboarding_primary_button")],
                        )
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "Missing visible element: splash_screen" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected recorder stderr to surface")


def test_execute_qa_demo_stage_retries_recording_failures() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    release = SimpleNamespace(
        commit_sha="b" * 40,
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[_demo_requirement()],
    )
    dev_result = DevResult(change_summary=["implemented"], pr_url="https://github.com/acme/repo/pull/8")
    test_result = TestResult(guidance=["pytest -q"])
    review_result = ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8")

    fake_agents = SimpleNamespace(qa=lambda **_: _qa_result())
    upload_attempts = {"count": 0}

    def _upload(**_kwargs):
        upload_attempts["count"] += 1
        if upload_attempts["count"] < 2:
            raise RuntimeError("temporary upload failure")
        return "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm"

    with (
        patch("orchestrator.core.qa.demo_service.build_codex_runtime"),
        patch("orchestrator.core.qa.demo_service.CodexWorkflowAgents", return_value=fake_agents),
        patch(
            "orchestrator.core.qa.demo_service.record_demo_scenarios",
            return_value=[
                _local_recording(name="Happy path"),
                _local_recording(name="Repeat action remains safe", path="/tmp/repeat.webm"),
            ],
        ),
        patch(
            "orchestrator.core.qa.demo_service.storage_config_from_settings",
            return_value=SimpleNamespace(
                endpoint="minio:9000",
                access_key="key",
                secret_key="secret",
                bucket="qa-demos",
                public_base_url="https://cdn.example/qa-demos",
                secure=False,
            ),
        ),
        patch("orchestrator.core.qa.demo_service.upload_recording", side_effect=_upload),
        patch("orchestrator.core.qa.demo_service._default_service_url_probe", return_value=200),
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200),
    ):
        result = execute_qa_demo_stage(
            session=SimpleNamespace(),
            settings=SimpleNamespace(qa_demo_playwright_module_dir="", qa_demo_max_attempts=2),
            tenant=tenant,
            project=project,
            run=run,
            request=_request(),
            plan=plan,
            dev_result=dev_result,
            test_result=test_result,
            review_result=review_result,
            preview_release=release,
        )

    assert upload_attempts["count"] == 3
    assert result.recordings[0].artifact_url.endswith("qa-demo-1.webm")


def test_update_pull_request_with_demo_evidence_refreshes_pr_body() -> None:
    class _GitHubClient:
        body = "## Summary\n- change"

        def get_pull_request_details(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                title="MAB-400: Add QA demos",
                body=self.body,
                base_ref="main",
            )

        def update_pull_request(self, **kwargs: object) -> object:
            self.body = str(kwargs["body"])
            return kwargs

    github_client = _GitHubClient()
    with (
        patch("orchestrator.core.qa.demo_service.github_client_from_tenant_config", return_value=github_client),
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200),
    ):
        updated_body = update_pull_request_with_demo_evidence(
            session=SimpleNamespace(),
            settings=_qa_artifact_settings(),
            tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
            project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
            run=SimpleNamespace(run_id="run-1"),
            workflow_result=SimpleNamespace(pr_url="https://github.com/acme/repo/pull/8"),
            qa_result=QaResult(
                summary=["Recorded demos"],
                scenarios=[
                    QaScenario(
                        name="Happy path",
                        objective="Show feature works",
                        steps=_proof_steps("text=Feature"),
                    )
                ],
                recordings=[
                    QaRecording(
                        name="Happy path",
                        artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/happy.webm",
                        object_key="tenant-1/project-1/run-1/happy.webm",
                        capture_reference="https://preview.example",
                        content_sha256=_sha256(30),
                        release_context_sha256=_release_context_sha256(),
                    )
                ],
            ),
            required_capture_targets=("browser",),
        )

    assert DEMO_EVIDENCE_HEADING in updated_body
    assert f"{DEMO_EVIDENCE_REQUIRED_TARGETS_MARKER} browser -->" in updated_body
    assert "https://cdn.example/qa-demos/tenant-1/project-1/run-1/happy.webm" in updated_body


def test_update_pull_request_with_demo_evidence_verifies_pr_readback_after_update() -> None:
    github_client = SimpleNamespace(
        get_pull_request_details=MagicMock(
            return_value=SimpleNamespace(
                title="MAB-400: Add QA demos",
                body="## Summary\n- change",
                base_ref="main",
            )
        ),
        update_pull_request=MagicMock(return_value=SimpleNamespace(number=8, html_url="https://github.com/acme/repo/pull/8")),
    )

    with (
        patch("orchestrator.core.qa.demo_service.github_client_from_tenant_config", return_value=github_client),
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200),
    ):
        try:
            update_pull_request_with_demo_evidence(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
                run=SimpleNamespace(run_id="run-1"),
                workflow_result=SimpleNamespace(pr_url="https://github.com/acme/repo/pull/8"),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Happy path",
                            objective="Show feature works",
                            steps=_proof_steps("text=Feature"),
                        )
                    ],
                    recordings=[
                        QaRecording(
                            name="Happy path",
                            artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/happy.webm",
                            object_key="tenant-1/project-1/run-1/happy.webm",
                            capture_reference="https://preview.example",
                            content_sha256=_sha256(46),
                            release_context_sha256=_release_context_sha256(),
                        )
                    ],
                ),
                required_capture_targets=("browser",),
                required_recording_counts={"browser": 1},
            )
        except RuntimeError as exc:
            assert "QA demo evidence PR update did not persist required evidence" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected stale PR readback to block evidence attachment")

    assert github_client.update_pull_request.called
    assert github_client.get_pull_request_details.call_count == 2


def test_update_pull_request_with_demo_evidence_rejects_missing_release_context_before_url_probe() -> None:
    with (
        patch("orchestrator.core.qa.demo_service.github_client_from_tenant_config") as github_client_mock,
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe") as url_probe,
    ):
        try:
            update_pull_request_with_demo_evidence(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
                run=SimpleNamespace(run_id="run-1"),
                workflow_result=SimpleNamespace(pr_url="https://github.com/acme/repo/pull/8"),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Browser walkthrough",
                            objective="Show browser",
                            capture_target="browser",
                            steps=_proof_steps("text=Feature"),
                        )
                    ],
                    recordings=[
                        SimpleNamespace(
                            name="Browser walkthrough",
                            artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                            object_key="tenant-1/project-1/run-1/browser.webm",
                            capture_target="browser",
                            capture_reference="https://preview.example",
                            content_sha256=_sha256(47),
                        )
                    ],
                ),
                required_capture_targets=("browser",),
            )
        except RuntimeError as exc:
            assert "QA demo recording release context sha256 is required before PR evidence" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected missing release context metadata to block PR evidence update")

    url_probe.assert_not_called()
    github_client_mock.assert_not_called()


def test_update_pull_request_with_demo_evidence_rejects_pr_url_for_another_repository() -> None:
    with (
        patch("orchestrator.core.qa.demo_service.github_client_from_tenant_config") as github_client_mock,
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200),
    ):
        try:
            update_pull_request_with_demo_evidence(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
                run=SimpleNamespace(run_id="run-1"),
                workflow_result=SimpleNamespace(pr_url="https://github.com/other/repo/pull/8"),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Browser walkthrough",
                            objective="Show browser",
                            capture_target="browser",
                            steps=_proof_steps("text=Feature"),
                        )
                    ],
                    recordings=[
                        QaRecording(
                            name="Browser walkthrough",
                            artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                            object_key="tenant-1/project-1/run-1/browser.webm",
                            capture_target="browser",
                            capture_reference="https://preview.example",
                            content_sha256=_sha256(31),
                            release_context_sha256=_release_context_sha256(),
                        )
                    ],
                ),
                required_capture_targets=("browser",),
            )
        except RuntimeError as exc:
            assert "QA demo recording PR URL must match project repository" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected cross-repository PR URL to block PR evidence update")

    github_client_mock.assert_not_called()


def test_update_pull_request_with_demo_evidence_rejects_external_recording_before_url_probe() -> None:
    with (
        patch("orchestrator.core.qa.demo_service.github_client_from_tenant_config") as github_client_mock,
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe") as url_probe,
    ):
        url_probe.return_value = 200
        github_client_mock.return_value.get_pull_request_details.return_value = SimpleNamespace(
            body="## Summary\n- change",
            title="Demo PR",
            base_ref="main",
        )
        try:
            update_pull_request_with_demo_evidence(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
                run=SimpleNamespace(run_id="run-1"),
                workflow_result=SimpleNamespace(pr_url="https://github.com/acme/repo/pull/8"),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Browser walkthrough",
                            objective="Show browser",
                            capture_target="browser",
                            steps=_proof_steps("text=Feature"),
                        )
                    ],
                    recordings=[
                        QaRecording(
                            name="Browser walkthrough",
                            artifact_url="https://manual.example/browser.webm",
                            object_key="tenant-1/project-1/run-1/browser.webm",
                            capture_target="browser",
                            capture_reference="https://preview.example",
                            content_sha256=_sha256(32),
                            release_context_sha256=_release_context_sha256(),
                        )
                    ],
                ),
                required_capture_targets=("browser",),
            )
        except RuntimeError as exc:
            assert "QA demo recording must use configured artifact storage URL" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected external recording URL to block PR evidence update")

    url_probe.assert_not_called()
    github_client_mock.assert_not_called()


def test_update_pull_request_with_demo_evidence_rejects_out_of_scope_recording_key_before_url_probe() -> None:
    with (
        patch("orchestrator.core.qa.demo_service.github_client_from_tenant_config") as github_client_mock,
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe") as url_probe,
    ):
        url_probe.return_value = 200
        github_client_mock.return_value.get_pull_request_details.return_value = SimpleNamespace(
            body="## Summary\n- change",
            title="Demo PR",
            base_ref="main",
        )
        try:
            update_pull_request_with_demo_evidence(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
                run=SimpleNamespace(run_id="run-1"),
                workflow_result=SimpleNamespace(pr_url="https://github.com/acme/repo/pull/8"),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Browser walkthrough",
                            objective="Show browser",
                            capture_target="browser",
                            steps=_proof_steps("text=Feature"),
                        )
                    ],
                    recordings=[
                        QaRecording(
                            name="Browser walkthrough",
                            artifact_url="https://cdn.example/qa-demos/tenant-2/project-2/run-2/browser.webm",
                            object_key="tenant-2/project-2/run-2/browser.webm",
                            capture_target="browser",
                            capture_reference="https://preview.example",
                            content_sha256=_sha256(33),
                            release_context_sha256=_release_context_sha256(),
                        )
                    ],
                ),
                required_capture_targets=("browser",),
            )
        except RuntimeError as exc:
            assert "QA demo recording object key must be scoped to this run" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected out-of-scope recording key to block PR evidence update")

    url_probe.assert_not_called()
    github_client_mock.assert_not_called()


def test_update_pull_request_with_demo_evidence_rejects_recording_url_key_mismatch_before_url_probe() -> None:
    with (
        patch("orchestrator.core.qa.demo_service.github_client_from_tenant_config") as github_client_mock,
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe") as url_probe,
    ):
        url_probe.return_value = 200
        github_client_mock.return_value.get_pull_request_details.return_value = SimpleNamespace(
            body="## Summary\n- change",
            title="Demo PR",
            base_ref="main",
        )
        try:
            update_pull_request_with_demo_evidence(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
                run=SimpleNamespace(run_id="run-1"),
                workflow_result=SimpleNamespace(pr_url="https://github.com/acme/repo/pull/8"),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Browser walkthrough",
                            objective="Show browser",
                            capture_target="browser",
                            steps=_proof_steps("text=Feature"),
                        )
                    ],
                    recordings=[
                        QaRecording(
                            name="Browser walkthrough",
                            artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/other.webm",
                            object_key="tenant-1/project-1/run-1/browser.webm",
                            capture_target="browser",
                            capture_reference="https://preview.example",
                            content_sha256=_sha256(34),
                            release_context_sha256=_release_context_sha256(),
                        )
                    ],
                ),
                required_capture_targets=("browser",),
            )
        except RuntimeError as exc:
            assert "QA demo recording URL must match its uploaded object key" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected URL/object-key mismatch to block PR evidence update")

    url_probe.assert_not_called()
    github_client_mock.assert_not_called()


def test_update_pull_request_with_demo_evidence_rejects_unsafe_metadata_before_url_probe() -> None:
    with (
        patch("orchestrator.core.qa.demo_service.github_client_from_tenant_config") as github_client_mock,
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe") as url_probe,
    ):
        url_probe.return_value = 200
        github_client_mock.return_value.get_pull_request_details.return_value = SimpleNamespace(
            body="## Summary\n- change",
            title="Demo PR",
            base_ref="main",
        )
        try:
            update_pull_request_with_demo_evidence(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
                run=SimpleNamespace(run_id="run-1"),
                workflow_result=SimpleNamespace(pr_url="https://github.com/acme/repo/pull/8"),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Browser walkthrough\n- Injected",
                            objective="Show browser",
                            capture_target="browser",
                            steps=_proof_steps("text=Feature"),
                        )
                    ],
                    recordings=[
                        QaRecording(
                            name="Browser walkthrough\n- Injected",
                            artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                            object_key="tenant-1/project-1/run-1/browser.webm",
                            capture_target="browser",
                            capture_reference="https://preview.example",
                            content_sha256=_sha256(35),
                            release_context_sha256=_release_context_sha256(),
                        )
                    ],
                ),
                required_capture_targets=("browser",),
                required_recording_counts={"browser": 1},
            )
        except RuntimeError as exc:
            assert "QA demo evidence recording metadata is not serializable" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected unsafe recording metadata to block PR evidence update")

    url_probe.assert_not_called()
    github_client_mock.assert_not_called()


def test_update_pull_request_with_demo_evidence_rejects_missing_required_capture_target_before_url_probe() -> None:
    with (
        patch("orchestrator.core.qa.demo_service.github_client_from_tenant_config") as github_client_mock,
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe") as url_probe,
    ):
        try:
            update_pull_request_with_demo_evidence(
                session=SimpleNamespace(),
                settings=SimpleNamespace(secrets_encryption_key="", qa_demo_artifact_url_timeout_seconds=1),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
                run=SimpleNamespace(run_id="run-1"),
                workflow_result=SimpleNamespace(pr_url="https://github.com/acme/repo/pull/8"),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Browser walkthrough",
                            objective="Show browser",
                            capture_target="browser",
                            steps=_proof_steps("text=Feature"),
                        )
                    ],
                    recordings=[
                        QaRecording(
                            name="Browser walkthrough",
                            artifact_url="https://demo.example/browser.webm",
                            object_key="qa/browser.webm",
                            capture_target="browser",
                            capture_reference="https://preview.example",
                            content_sha256=_sha256(36),
                            release_context_sha256=_release_context_sha256(),
                        )
                    ],
                ),
                required_capture_targets=("browser", "ios"),
            )
        except RuntimeError as exc:
            assert "QA demo evidence is missing required capture target(s): ios" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected missing required capture target to block PR evidence update")

    url_probe.assert_not_called()
    github_client_mock.assert_not_called()


def test_update_pull_request_with_demo_evidence_rejects_missing_required_recording_count_before_url_probe() -> None:
    with (
        patch("orchestrator.core.qa.demo_service.github_client_from_tenant_config") as github_client_mock,
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe") as url_probe,
    ):
        try:
            update_pull_request_with_demo_evidence(
                session=SimpleNamespace(),
                settings=SimpleNamespace(secrets_encryption_key="", qa_demo_artifact_url_timeout_seconds=1),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
                run=SimpleNamespace(run_id="run-1"),
                workflow_result=SimpleNamespace(pr_url="https://github.com/acme/repo/pull/8"),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Browser happy path",
                            objective="Show browser happy path",
                            capture_target="browser",
                            steps=_proof_steps("text=Feature"),
                        )
                    ],
                    recordings=[
                        QaRecording(
                            name="Browser happy path",
                            artifact_url="https://demo.example/browser.webm",
                            object_key="qa/browser.webm",
                            capture_target="browser",
                            capture_reference="https://preview.example",
                            content_sha256=_sha256(37),
                            release_context_sha256=_release_context_sha256(),
                        )
                    ],
                ),
                required_capture_targets=("browser",),
                required_recording_counts={"browser": 2},
            )
        except RuntimeError as exc:
            assert "QA demo evidence is missing required recording count(s): browser requires 2, recorded 1" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected missing required recording count to block PR evidence update")

    url_probe.assert_not_called()
    github_client_mock.assert_not_called()


def test_update_pull_request_with_demo_evidence_rejects_unreachable_accumulated_recording() -> None:
    with patch(
        "orchestrator.core.qa.demo_service._default_artifact_url_probe",
        side_effect=RuntimeError("object expired"),
    ):
        try:
            update_pull_request_with_demo_evidence(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
                run=SimpleNamespace(run_id="run-1"),
                workflow_result=SimpleNamespace(pr_url="https://github.com/acme/repo/pull/8"),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Browser walkthrough",
                            objective="Show browser",
                            capture_target="browser",
                            steps=_proof_steps("text=Feature"),
                        ),
                        QaScenario(
                            name="iOS walkthrough",
                            objective="Show iOS",
                            capture_target="ios",
                            steps=_proof_steps("text=Ready"),
                        ),
                    ],
                    recordings=[
                        QaRecording(
                            name="Browser walkthrough",
                            artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                            object_key="tenant-1/project-1/run-1/browser.webm",
                            capture_reference="https://preview.example",
                            content_sha256=_sha256(38),
                            release_context_sha256=_release_context_sha256(),
                        ),
                        QaRecording(
                            name="iOS walkthrough",
                            artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios.mp4",
                            object_key="tenant-1/project-1/run-1/ios.mp4",
                            capture_target="ios",
                            capture_reference="ios-simulator://configured",
                            content_sha256=_sha256(39),
                            release_context_sha256=_release_context_sha256(),
                        ),
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "QA demo artifact URL is not reachable" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected unreachable accumulated recording to block PR evidence update")


def test_update_pull_request_with_demo_evidence_rejects_recording_without_matching_scenario() -> None:
    with patch("orchestrator.core.qa.demo_service._default_artifact_url_probe") as url_probe:
        try:
            update_pull_request_with_demo_evidence(
                session=SimpleNamespace(),
                settings=SimpleNamespace(secrets_encryption_key="", qa_demo_artifact_url_timeout_seconds=1),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
                run=SimpleNamespace(run_id="run-1"),
                workflow_result=SimpleNamespace(pr_url="https://github.com/acme/repo/pull/8"),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[],
                    recordings=[
                        QaRecording(
                            name="Browser walkthrough",
                            artifact_url="https://demo.example/browser.webm",
                            object_key="qa/browser.webm",
                            capture_target="browser",
                            capture_reference="https://preview.example",
                            content_sha256=_sha256(40),
                            release_context_sha256=_release_context_sha256(),
                        )
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "missing matching executable scenario(s): browser: Browser walkthrough" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected orphaned recording proof to block PR evidence update")

    url_probe.assert_not_called()


def test_update_pull_request_with_demo_evidence_rejects_duplicate_accumulated_recording_key() -> None:
    with patch("orchestrator.core.qa.demo_service._default_artifact_url_probe") as url_probe:
        try:
            update_pull_request_with_demo_evidence(
                session=SimpleNamespace(),
                settings=SimpleNamespace(secrets_encryption_key="", qa_demo_artifact_url_timeout_seconds=1),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
                run=SimpleNamespace(run_id="run-1"),
                workflow_result=SimpleNamespace(pr_url="https://github.com/acme/repo/pull/8"),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Browser walkthrough",
                            objective="Show browser",
                            capture_target="browser",
                            steps=_proof_steps("text=Feature"),
                        )
                    ],
                    recordings=[
                        QaRecording(
                            name="Browser walkthrough",
                            artifact_url="https://demo.example/browser-1.webm",
                            object_key="qa/browser-1.webm",
                            capture_target="browser",
                            capture_reference="https://preview.example",
                            content_sha256=_sha256(41),
                            release_context_sha256=_release_context_sha256(),
                        ),
                        QaRecording(
                            name="Browser walkthrough",
                            artifact_url="https://demo.example/browser-2.webm",
                            object_key="qa/browser-2.webm",
                            capture_target="browser",
                            capture_reference="https://preview.example",
                            content_sha256=_sha256(42),
                            release_context_sha256=_release_context_sha256(),
                        ),
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "recording proof keys must be unique before PR evidence: browser: Browser walkthrough" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected duplicate recording proof key to block PR evidence update")

    url_probe.assert_not_called()


def test_update_pull_request_with_demo_evidence_rejects_duplicate_object_key_before_url_probe() -> None:
    with (
        patch("orchestrator.core.qa.demo_service.github_client_from_tenant_config") as github_client_mock,
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe") as url_probe,
    ):
        try:
            update_pull_request_with_demo_evidence(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
                run=SimpleNamespace(run_id="run-1"),
                workflow_result=SimpleNamespace(pr_url="https://github.com/acme/repo/pull/8"),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Browser happy path",
                            objective="Show browser happy path",
                            capture_target="browser",
                            steps=_proof_steps("text=Feature"),
                        ),
                        QaScenario(
                            name="Browser edge case",
                            objective="Show browser edge case",
                            capture_target="browser",
                            steps=_proof_steps("text=Edge"),
                        ),
                    ],
                    recordings=[
                        QaRecording(
                            name="Browser happy path",
                            artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                            object_key="tenant-1/project-1/run-1/browser.webm",
                            capture_target="browser",
                            capture_reference="https://preview.example",
                            content_sha256=_sha256(43),
                            release_context_sha256=_release_context_sha256(),
                        ),
                        QaRecording(
                            name="Browser edge case",
                            artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                            object_key="tenant-1/project-1/run-1/browser.webm",
                            capture_target="browser",
                            capture_reference="https://preview.example",
                            content_sha256=_sha256(44),
                            release_context_sha256=_release_context_sha256(),
                        ),
                    ],
                ),
                required_capture_targets=("browser",),
                required_recording_counts={"browser": 2},
            )
        except RuntimeError as exc:
            assert "QA demo recording object keys must be unique before PR evidence" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected duplicate object key to block PR evidence update")

    url_probe.assert_not_called()
    github_client_mock.assert_not_called()


def test_update_pull_request_with_demo_evidence_rejects_missing_content_sha256_before_url_probe() -> None:
    with (
        patch("orchestrator.core.qa.demo_service.github_client_from_tenant_config") as github_client_mock,
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe") as url_probe,
    ):
        url_probe.return_value = 200
        github_client_mock.return_value.get_pull_request_details.return_value = SimpleNamespace(
            body="## Summary\n- change",
            title="Demo PR",
            base_ref="main",
        )
        try:
            update_pull_request_with_demo_evidence(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
                run=SimpleNamespace(run_id="run-1"),
                workflow_result=SimpleNamespace(pr_url="https://github.com/acme/repo/pull/8"),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Browser happy path",
                            objective="Show browser happy path",
                            capture_target="browser",
                            steps=_proof_steps("text=Feature"),
                        )
                    ],
                    recordings=[
                        QaRecording(
                            name="Browser happy path",
                            artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                            object_key="tenant-1/project-1/run-1/browser.webm",
                            capture_target="browser",
                            capture_reference="https://preview.example",
                        )
                    ],
                ),
                required_capture_targets=("browser",),
                required_recording_counts={"browser": 1},
            )
        except RuntimeError as exc:
            assert "QA demo recording content sha256 is required before PR evidence" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected missing content sha256 to block PR evidence update")

    url_probe.assert_not_called()
    github_client_mock.assert_not_called()


def test_update_pull_request_with_demo_evidence_rejects_duplicate_content_sha256_before_url_probe() -> None:
    duplicated_digest = "a" * 64
    recordings = [
        SimpleNamespace(
            name="Browser happy path",
            artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-1.webm",
            object_key="tenant-1/project-1/run-1/browser-1.webm",
            capture_target="browser",
            capture_reference="https://preview.example",
            content_sha256=duplicated_digest,
            release_context_sha256=_release_context_sha256(),
        ),
        SimpleNamespace(
            name="Browser edge case",
            artifact_url="https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-2.webm",
            object_key="tenant-1/project-1/run-1/browser-2.webm",
            capture_target="browser",
            capture_reference="https://preview.example",
            content_sha256=duplicated_digest,
            release_context_sha256=_release_context_sha256(),
        ),
    ]
    with (
        patch("orchestrator.core.qa.demo_service.github_client_from_tenant_config") as github_client_mock,
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe") as url_probe,
    ):
        url_probe.return_value = 200
        github_client_mock.return_value.get_pull_request_details.return_value = SimpleNamespace(
            body="## Summary\n- change",
            title="Demo PR",
            base_ref="main",
        )
        try:
            update_pull_request_with_demo_evidence(
                session=SimpleNamespace(),
                settings=_qa_artifact_settings(),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
                run=SimpleNamespace(run_id="run-1"),
                workflow_result=SimpleNamespace(pr_url="https://github.com/acme/repo/pull/8"),
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Browser happy path",
                            objective="Show browser happy path",
                            capture_target="browser",
                            steps=_proof_steps("text=Feature"),
                        ),
                        QaScenario(
                            name="Browser edge case",
                            objective="Show browser edge case",
                            capture_target="browser",
                            steps=_proof_steps("text=Edge"),
                        ),
                    ],
                    recordings=recordings,  # type: ignore[arg-type]
                ),
                required_capture_targets=("browser",),
                required_recording_counts={"browser": 2},
            )
        except RuntimeError as exc:
            assert "QA demo recording content sha256 values must be unique before PR evidence" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected duplicate content sha256 to block PR evidence update")

    url_probe.assert_not_called()
    github_client_mock.assert_not_called()


def test_artifact_url_validation_rejects_no_content_response_as_demo_proof() -> None:
    try:
        ensure_artifact_url_reachable(
            "https://demo.example/empty.webm",
            artifact_url_probe=lambda _url, **_kwargs: 204,
            timeout_seconds=1,
        )
    except RuntimeError as exc:
        assert "did not return playable video evidence" in str(exc)
        assert "HTTP 204" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected no-content artifact URL to be rejected")
