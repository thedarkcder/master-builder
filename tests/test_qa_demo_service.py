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
    DEMO_EVIDENCE_REQUIRED_TARGETS_MARKER,
    DemoCaptureTarget,
    ensure_artifact_url_reachable,
    ensure_release_ready_for_qa,
    ensure_capture_target_runtime_ready,
    execute_qa_demo_stage,
    planned_capture_target_constraints_payload,
    qa_demo_max_attempts,
    qa_demo_recording_enabled,
    record_demo_scenarios,
    remaining_capture_targets,
    required_capture_targets,
    required_recording_counts_by_target,
    required_release_service_kinds,
    resolve_available_capture_targets,
    resolve_preview_demo_url,
    storage_config_from_settings,
    update_pull_request_with_demo_evidence,
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


def _qa_result() -> QaResult:
    return QaResult(
        summary=["Recorded demos"],
        scenarios=[
            QaScenario(
                name="Happy path",
                objective="Show feature works",
                steps=[QaStep(action="goto", value="/"), QaStep(action="assert_visible", selector="text=Feature")],
            )
        ],
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


def test_qa_demo_recording_enabled_reads_effective_policy() -> None:
    assert qa_demo_recording_enabled({"qa_demo_recording_enabled": True}) is True
    assert qa_demo_recording_enabled({"qa_demo_recording_enabled": False}) is False
    assert qa_demo_recording_enabled({}) is False


def test_qa_demo_max_attempts_defaults_and_caps() -> None:
    assert qa_demo_max_attempts(SimpleNamespace()) == 3
    assert qa_demo_max_attempts(SimpleNamespace(qa_demo_max_attempts=0)) == 1
    assert qa_demo_max_attempts(SimpleNamespace(qa_demo_max_attempts="5")) == 5


def test_storage_config_from_settings_requires_complete_configuration() -> None:
    try:
        storage_config_from_settings(SimpleNamespace())
    except RuntimeError as exc:
        assert "not fully configured" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected storage config validation failure")


def test_resolve_preview_demo_url_prefers_active_website() -> None:
    release = SimpleNamespace(
        service_urls=[
            SimpleNamespace(service_kind="api", status="active", url="https://api.example"),
            SimpleNamespace(service_kind="website", status="active", url="https://preview.example"),
        ]
    )
    assert resolve_preview_demo_url(release) == "https://preview.example"


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
            ),
            QaRecording(
                name="iOS walkthrough",
                artifact_url="https://cdn.example/ios-1.mp4",
                object_key="tenant/project/run/ios-1.mp4",
                capture_target="ios",
                capture_reference="ios-simulator://configured",
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
                ),
                QaRecording(
                    name="Browser invalid input",
                    artifact_url="https://cdn.example/browser-2.webm",
                    object_key="tenant/project/run/browser-2.webm",
                    capture_target="browser",
                    capture_reference="https://preview.example",
                ),
                QaRecording(
                    name="Browser repeat action",
                    artifact_url="https://cdn.example/browser-3.webm",
                    object_key="tenant/project/run/browser-3.webm",
                    capture_target="browser",
                    capture_reference="https://preview.example",
                ),
                QaRecording(
                    name="iOS walkthrough",
                    artifact_url="https://cdn.example/ios-1.mp4",
                    object_key="tenant/project/run/ios-1.mp4",
                    capture_target="ios",
                    capture_reference="ios-simulator://configured",
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
        preview_release=SimpleNamespace(service_urls=[]),
    )

    assert set(targets) == {"ios", "android", "desktop"}
    assert targets["ios"].required_worker_platform == "macos"
    assert targets["android"].required_worker_platform == "linux"
    assert targets["android"].recorder_command == ("python", "/tmp/android_recorder.py")
    assert targets["desktop"].recorder_command == ("python", "/tmp/desktop_recorder.py")
    assert targets["desktop"].required_worker_platform == "macos"


def test_resolve_available_capture_targets_rejects_invalid_desktop_worker_platform() -> None:
    try:
        resolve_available_capture_targets(
            settings=SimpleNamespace(
                qa_demo_desktop_recorder_command="python /tmp/desktop_recorder.py",
                qa_demo_desktop_worker_platform="windows",
            ),
            preview_release=SimpleNamespace(service_urls=[]),
        )
    except RuntimeError as exc:
        assert "desktop worker platform is invalid" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected invalid desktop worker platform failure")


def test_resolve_available_capture_targets_includes_builtin_ios_and_android_recorders_when_unconfigured() -> None:
    targets = resolve_available_capture_targets(
        settings=SimpleNamespace(),
        preview_release=SimpleNamespace(service_urls=[]),
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
            )
        ],
    )
    assert DEMO_EVIDENCE_HEADING in updated
    assert DEMO_EVIDENCE_MARKER in updated
    assert "[target=browser; reference=https://preview.example; object_key=qa/happy.webm]" in updated
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
            ),
            QaRecording(
                name="iOS walkthrough",
                artifact_url="https://demo.example/ios.mp4",
                object_key="qa/ios.mp4",
                capture_target="ios",
                capture_reference="ios-simulator://configured",
            ),
        ],
        required_capture_targets=("browser", "ios", "android"),
    )

    assert f"{DEMO_EVIDENCE_REQUIRED_TARGETS_MARKER} browser,ios,android -->" in updated


def test_record_demo_scenarios_passes_explicit_playwright_module_dir() -> None:
    commands: list[tuple[list[str], dict[str, str]]] = []

    def _run(cmd, **kwargs):  # noqa: ANN001
        commands.append((list(cmd), dict(kwargs.get("env") or {})))
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

    assert len(recordings) == 1
    assert Path(recordings[0].path).exists()
    assert commands[0][1]["QA_DEMO_PLAYWRIGHT_MODULE_DIR"] == "/tmp/playwright-modules"


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
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[DemoRequirement(title="Feature walkthrough", acceptance_criterion="Feature works")],
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
                SimpleNamespace(
                    name="Happy path",
                    path="/tmp/happy.webm",
                    capture_target="browser",
                    capture_reference="https://preview.example",
                    content_type="video/webm",
                )
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
    available_targets = json.loads(fake_agents.qa.call_args.kwargs["available_capture_targets_json"])
    assert {"capture_target": "browser", "capture_reference": "https://preview.example"} in available_targets
    assert upload_mock.call_args.kwargs["content_type"] == "video/webm"


def test_execute_qa_demo_stage_blocks_when_qa_scenarios_do_not_cover_pm_variants() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    release = SimpleNamespace(
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
            assert "browser: expected at least 3, got 1" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected QA scenario variant coverage failure")

    record_mock.assert_not_called()


def test_execute_qa_demo_stage_blocks_when_qa_scenarios_do_not_name_pm_variant_coverage() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    release = SimpleNamespace(
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
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[DemoRequirement(title="Feature walkthrough", acceptance_criterion="Feature works")],
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
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[DemoRequirement(title="Feature walkthrough", acceptance_criterion="Feature works")],
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
                SimpleNamespace(
                    name="Happy path",
                    path="/tmp/happy.webm",
                    capture_target="browser",
                    capture_reference="https://preview.example",
                    content_type="video/webm",
                )
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

    assert probe_attempts["count"] == 2
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
            DemoRequirement(title="Native walkthrough", acceptance_criterion="Feature works", capture_target="ios")
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
                SimpleNamespace(
                    name="Mobile flow",
                    path="/tmp/mobile.mp4",
                    capture_target="ios",
                    capture_reference="ios-simulator://configured",
                    content_type="video/mp4",
                )
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
            preview_release=SimpleNamespace(service_urls=[]),
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
            DemoRequirement(title="Native walkthrough", acceptance_criterion="Feature works", capture_target="ios")
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
                SimpleNamespace(
                    name="Mobile flow",
                    path="/tmp/mobile.mp4",
                    capture_target="ios",
                    capture_reference="ios-simulator://configured",
                    content_type="video/mp4",
                )
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
            preview_release=SimpleNamespace(service_urls=[]),
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
            DemoRequirement(title="Native walkthrough", acceptance_criterion="Feature works", capture_target="ios")
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
                preview_release=SimpleNamespace(service_urls=[]),
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
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works on iOS"],
        risks=[],
        demo_requirements=[DemoRequirement(title="Native walkthrough", acceptance_criterion="Feature works on iOS", capture_target="ios")],
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
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works everywhere"],
        risks=[],
        demo_requirements=[
            DemoRequirement(title="Browser walkthrough", acceptance_criterion="Feature works everywhere", capture_target="browser"),
            DemoRequirement(title="iOS walkthrough", acceptance_criterion="Feature works everywhere", capture_target="ios"),
            DemoRequirement(title="Android walkthrough", acceptance_criterion="Feature works everywhere", capture_target="android"),
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
                        name="Android walkthrough",
                        objective="Show Android",
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
                SimpleNamespace(
                    name="Browser walkthrough",
                    path="/tmp/browser.webm",
                    capture_target="browser",
                    capture_reference="https://preview.example",
                    content_type="video/webm",
                ),
                SimpleNamespace(
                    name="Android walkthrough",
                    path="/tmp/android.mp4",
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
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-2.mp4",
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
    assert [recording.capture_target for recording in result.recordings] == ["browser", "android"]
    assert "still requires capture target(s): ios" in str(result.feedback)
    available_targets = json.loads(fake_agents.qa.call_args.kwargs["available_capture_targets_json"])
    assert [item["capture_target"] for item in available_targets] == ["browser", "android"]


def test_execute_qa_demo_stage_blocks_when_current_worker_target_proof_is_missing() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    release = SimpleNamespace(
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works on browser and Android"],
        risks=[],
        demo_requirements=[
            DemoRequirement(title="Browser walkthrough", acceptance_criterion="Feature works on browser", capture_target="browser"),
            DemoRequirement(title="Android walkthrough", acceptance_criterion="Feature works on Android", capture_target="android"),
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
            assert "insufficient scenario count: android: expected at least 1, got 0" in str(exc)
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
            DemoRequirement(title="Browser walkthrough", acceptance_criterion="Feature works everywhere", capture_target="browser"),
            DemoRequirement(title="iOS walkthrough", acceptance_criterion="Feature works everywhere", capture_target="ios"),
            DemoRequirement(title="Android walkthrough", acceptance_criterion="Feature works everywhere", capture_target="android"),
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
                name="Android walkthrough",
                objective="Show Android",
                capture_target="android",
                steps=_proof_steps("text=Ready"),
            ),
        ],
        recordings=[
            QaRecording(
                name="Browser walkthrough",
                artifact_url="https://cdn.example/qa-demo-1.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
            ),
            QaRecording(
                name="Android walkthrough",
                artifact_url="https://cdn.example/qa-demo-2.mp4",
                object_key="tenant-1/project-1/run-1/qa-demo-2.mp4",
                capture_target="android",
                capture_reference="android-emulator://configured",
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
                SimpleNamespace(
                    name="iOS walkthrough",
                    path="/tmp/ios.mp4",
                    capture_target="ios",
                    capture_reference="ios-simulator://configured",
                    content_type="video/mp4",
                )
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
            preview_release=SimpleNamespace(service_urls=[]),
            previous_qa_result=previous_qa,
        )

    assert result.outcome == "continue"
    assert [recording.capture_target for recording in result.recordings] == ["browser", "android", "ios"]
    assert upload_mock.call_args.kwargs["object_key"].endswith("qa-demo-3.mp4")
    available_targets = json.loads(fake_agents.qa.call_args.kwargs["available_capture_targets_json"])
    assert available_targets == [{"capture_target": "ios", "capture_reference": "ios-simulator://configured"}]


def test_execute_qa_demo_stage_rejects_previous_recording_without_matching_scenario() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works on browser"],
        risks=[],
        demo_requirements=[
            DemoRequirement(title="Browser walkthrough", acceptance_criterion="Feature works on browser", capture_target="browser"),
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
                preview_release=SimpleNamespace(service_urls=[]),
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
        demo_requirements=[DemoRequirement(title="Native walkthrough", acceptance_criterion="Feature works on iOS", capture_target="ios")],
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
                SimpleNamespace(
                    name="Native walkthrough",
                    path="/tmp/native.mp4",
                    capture_target="ios",
                    capture_reference="ios-simulator://configured",
                    content_type="video/mp4",
                )
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
            preview_release=SimpleNamespace(service_urls=[]),
        )

    assert result.recordings[0].artifact_url.endswith("qa-demo-1.mp4")
    assert result.recordings[0].capture_target == "ios"
    assert fake_agents.qa.call_args.kwargs["available_capture_targets_json"] == (
        '[{"capture_target": "ios", "capture_reference": "ios-simulator://configured"}]'
    )


def test_execute_qa_demo_stage_uses_builtin_android_capture_on_linux() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works on Android"],
        risks=[],
        demo_requirements=[
            DemoRequirement(
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
                SimpleNamespace(
                    name="Android walkthrough",
                    path="/tmp/android.mp4",
                    capture_target="android",
                    capture_reference="android-emulator://configured",
                    content_type="video/mp4",
                )
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
            preview_release=SimpleNamespace(service_urls=[]),
        )

    assert result.recordings[0].capture_target == "android"
    assert fake_agents.qa.call_args.kwargs["available_capture_targets_json"] == (
        '[{"capture_target": "android", "capture_reference": "android-emulator://configured"}]'
    )


def test_execute_qa_demo_stage_requeues_when_desktop_capture_requires_macos_worker() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo")
    run = SimpleNamespace(run_id="run-1")
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Desktop feature works"],
        risks=[],
        demo_requirements=[
            DemoRequirement(
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
        preview_release=SimpleNamespace(service_urls=[]),
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
        service_urls=[SimpleNamespace(service_kind="website", status="active", url="https://preview.example")]
    )
    plan = PmPlan(
        plan_steps=["Implement"],
        acceptance_criteria=["Feature works"],
        risks=[],
        demo_requirements=[DemoRequirement(title="Feature walkthrough", acceptance_criterion="Feature works")],
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
                SimpleNamespace(
                    name="Happy path",
                    path="/tmp/happy.webm",
                    capture_target="browser",
                    capture_reference="https://preview.example",
                    content_type="video/webm",
                )
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

    assert upload_attempts["count"] == 2
    assert result.recordings[0].artifact_url.endswith("qa-demo-1.webm")


def test_update_pull_request_with_demo_evidence_refreshes_pr_body() -> None:
    github_client = SimpleNamespace(
        get_pull_request_details=lambda **_: SimpleNamespace(
            title="MAB-400: Add QA demos",
            body="## Summary\n- change",
            base_ref="main",
        ),
        update_pull_request=lambda **kwargs: kwargs,
    )
    with (
        patch("orchestrator.core.qa.demo_service.github_client_from_tenant_config", return_value=github_client),
        patch("orchestrator.core.qa.demo_service._default_artifact_url_probe", return_value=200),
    ):
        updated_body = update_pull_request_with_demo_evidence(
            session=SimpleNamespace(),
            settings=SimpleNamespace(secrets_encryption_key=""),
            tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
            project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
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
                        artifact_url="https://demo.example/happy.webm",
                        object_key="qa/happy.webm",
                        capture_reference="https://preview.example",
                    )
                ],
            ),
            required_capture_targets=("browser",),
        )

    assert DEMO_EVIDENCE_HEADING in updated_body
    assert f"{DEMO_EVIDENCE_REQUIRED_TARGETS_MARKER} browser -->" in updated_body
    assert "https://demo.example/happy.webm" in updated_body


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


def test_update_pull_request_with_demo_evidence_rejects_unreachable_accumulated_recording() -> None:
    with patch(
        "orchestrator.core.qa.demo_service._default_artifact_url_probe",
        side_effect=RuntimeError("object expired"),
    ):
        try:
            update_pull_request_with_demo_evidence(
                session=SimpleNamespace(),
                settings=SimpleNamespace(secrets_encryption_key="", qa_demo_artifact_url_timeout_seconds=1),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="https://github.com/acme/repo"),
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
                            artifact_url="https://demo.example/browser.webm",
                            object_key="qa/browser.webm",
                            capture_reference="https://preview.example",
                        ),
                        QaRecording(
                            name="iOS walkthrough",
                            artifact_url="https://demo.example/ios.mp4",
                            object_key="qa/ios.mp4",
                            capture_target="ios",
                            capture_reference="ios-simulator://configured",
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
                        ),
                        QaRecording(
                            name="Browser walkthrough",
                            artifact_url="https://demo.example/browser-2.webm",
                            object_key="qa/browser-2.webm",
                            capture_target="browser",
                            capture_reference="https://preview.example",
                        ),
                    ],
                ),
            )
        except RuntimeError as exc:
            assert "recording proof keys must be unique before PR evidence: browser: Browser walkthrough" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected duplicate recording proof key to block PR evidence update")

    url_probe.assert_not_called()


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
