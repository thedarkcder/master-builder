from __future__ import annotations

import json
import signal
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.qa_demo_mobile_recorder import _ios_command_timeout_seconds
from scripts.qa_demo_mobile_recorder import _ios_failure_diagnostics
from scripts.qa_demo_mobile_recorder import _combined_recording_failure
from scripts.qa_demo_mobile_recorder import _build_for_testing
from scripts.qa_demo_mobile_recorder import _reboot_simulator
from scripts.qa_demo_mobile_recorder import _run
from scripts.qa_demo_mobile_recorder import discover_ios_project_files
from scripts.qa_demo_mobile_recorder import main
from scripts.qa_demo_mobile_recorder import qa_demo_launch_environment
from orchestrator.core.qa.mobile_xcuitest_recorder import render_xcuitest_source
from orchestrator.core.workflow.runner import QaScenario, QaStep


def test_combined_recording_failure_prefers_primary_error() -> None:
    primary = RuntimeError("xcodebuild failed")

    combined = _combined_recording_failure(primary_error=primary, stop_error=None)

    assert combined is primary


def test_combined_recording_failure_keeps_shutdown_error_when_primary_missing() -> None:
    stop_error = RuntimeError("recordVideo failed")

    combined = _combined_recording_failure(primary_error=None, stop_error=stop_error)

    assert combined is stop_error


def test_combined_recording_failure_surfaces_both_errors() -> None:
    combined = _combined_recording_failure(
        primary_error=RuntimeError("xcodebuild failed"),
        stop_error=RuntimeError("recordVideo failed"),
    )

    assert isinstance(combined, RuntimeError)
    assert "xcodebuild failed" in str(combined)
    assert "recordVideo failed" in str(combined)


def test_reboot_simulator_shuts_down_then_boots() -> None:
    calls: list[list[str]] = []

    def _record_run(args, **kwargs):  # noqa: ANN001
        calls.append(list(args))
        return None

    with (
        patch("scripts.qa_demo_mobile_recorder.subprocess.run", side_effect=_record_run),
        patch("scripts.qa_demo_mobile_recorder._ensure_simulator_booted") as ensure_mock,
    ):
        _reboot_simulator("SIM-123")

    assert calls == [["xcrun", "simctl", "shutdown", "SIM-123"]]
    ensure_mock.assert_called_once_with("SIM-123")


def test_ios_command_timeout_defaults_to_bounded_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QA_DEMO_IOS_COMMAND_TIMEOUT_SECONDS", raising=False)

    assert _ios_command_timeout_seconds() == 600


def test_ios_command_timeout_rejects_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QA_DEMO_IOS_COMMAND_TIMEOUT_SECONDS", "0")

    with pytest.raises(RuntimeError, match="greater than zero"):
        _ios_command_timeout_seconds()


def test_run_passes_bounded_timeout_to_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QA_DEMO_IOS_COMMAND_TIMEOUT_SECONDS", "123")
    captured: dict[str, object] = {}

    def _fake_run(args, **kwargs):  # noqa: ANN001
        captured["args"] = args
        captured["timeout"] = kwargs.get("timeout")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("scripts.qa_demo_mobile_recorder.subprocess.run", _fake_run)

    _run(["xcodebuild", "-version"], capture_output=True)

    assert captured["args"] == ["xcodebuild", "-version"]
    assert captured["timeout"] == 123


def test_run_surfaces_timeout_as_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QA_DEMO_IOS_COMMAND_TIMEOUT_SECONDS", "123")

    def _fake_run(args, **kwargs):  # noqa: ANN001
        raise TimeoutError("timer expired")

    monkeypatch.setattr("scripts.qa_demo_mobile_recorder.subprocess.run", _fake_run)

    with pytest.raises(RuntimeError, match="timed out after 123 seconds"):
        _run(["xcodebuild", "-version"], capture_output=True)


def test_ios_failure_diagnostics_include_simulator_crash_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result_bundle_path = tmp_path / "AppLoad.xcresult"
    calls: list[list[str]] = []

    def _fake_run(args, **_kwargs):  # noqa: ANN001
        calls.append(list(args))
        if "xcresulttool" in args:
            return type(
                "Result",
                (),
                {
                    "stdout": json.dumps(
                        {
                            "issues": {
                                "testFailureSummaries": [
                                    {
                                        "testCaseName": "App load",
                                        "message": "Ready was not visible",
                                    }
                                ]
                            }
                        }
                    ),
                    "stderr": "",
                    "returncode": 0,
                },
            )()
        if "spawn" in args and "log" in args:
            return type(
                "Result",
                (),
                {
                    "stdout": "\n".join(
                        [
                            "unrelated simulator noise",
                            "com.example.app Fatal error: unexpectedly found nil",
                            "EXC_CRASH SIGABRT com.example.app",
                        ]
                    ),
                    "stderr": "",
                    "returncode": 0,
                },
            )()
        return type("Result", (), {"stdout": "", "stderr": "", "returncode": 0})()

    monkeypatch.setattr("scripts.qa_demo_mobile_recorder._run", _fake_run)

    diagnostics = _ios_failure_diagnostics(
        result_bundle_path=result_bundle_path,
        simulator_udid="SIM-123",
        bundle_id="com.example.app",
    )

    assert "iOS diagnostics:" in diagnostics
    assert "Ready was not visible" in diagnostics
    assert "iOS simulator logs:" in diagnostics
    assert "Fatal error: unexpectedly found nil" in diagnostics
    assert "EXC_CRASH SIGABRT com.example.app" in diagnostics
    assert any(call[:4] == ["xcrun", "simctl", "spawn", "SIM-123"] for call in calls)


def test_qa_demo_launch_environment_maps_release_context() -> None:
    launch_environment = qa_demo_launch_environment(
        {
            "release_commit_sha": "b" * 40,
            "release_api_base_url": "https://api.preview.example",
            "release_browser_url": "https://web.preview.example",
            "release_service_urls": [
                {"service_kind": "api", "url": "https://api.preview.example"},
            ],
        }
    )

    assert launch_environment["MB_QA_DEMO_RELEASE_COMMIT_SHA"] == "b" * 40
    assert launch_environment["MB_QA_DEMO_RELEASE_API_BASE_URL"] == "https://api.preview.example"
    assert launch_environment["QA_DEMO_API_BASE_URL"] == "https://api.preview.example"
    assert launch_environment["QA_DEMO_BROWSER_URL"] == "https://web.preview.example"
    assert (
        launch_environment["MB_QA_DEMO_RELEASE_SERVICE_URLS_JSON"]
        == '[{"service_kind":"api","url":"https://api.preview.example"}]'
    )


def test_render_xcuitest_source_injects_release_context_launch_environment() -> None:
    source = render_xcuitest_source(
        test_class_name="exampleUITests",
        scenarios=[
            QaScenario(
                name="API-backed flow",
                objective="Prove app uses preview API",
                capture_target="ios",
                steps=[QaStep(action="assert_visible", selector="text=Ready")],
            )
        ],
        launch_environment={"QA_DEMO_API_BASE_URL": "https://api.preview.example"},
    )

    assert 'private let qaDemoLaunchEnvironment: [String: String] = ["QA_DEMO_API_BASE_URL": "https://api.preview.example"]' in source
    assert "app.launchEnvironment = qaDemoLaunchEnvironment" in source


def test_discover_ios_project_files_prefers_payload_source_paths(tmp_path: Path) -> None:
    ios_dir = tmp_path / "clients" / "ios"
    (ios_dir / "example.xcodeproj").mkdir(parents=True)
    ui_test_file = ios_dir / "exampleUITests" / "exampleUITests.swift"
    ui_test_file.parent.mkdir(parents=True)
    ui_test_file.write_text("import XCTest\n", encoding="utf-8")
    other_dir = tmp_path / "samples" / "ios"
    (other_dir / "Sample.xcodeproj").mkdir(parents=True)
    other_test_file = other_dir / "SampleUITests" / "SampleUITests.swift"
    other_test_file.parent.mkdir(parents=True)
    other_test_file.write_text("import XCTest\n", encoding="utf-8")

    project_path, discovered_test_file = discover_ios_project_files(
        repo_dir=tmp_path,
        target_source_paths=["clients/ios"],
    )

    assert project_path == ios_dir / "example.xcodeproj"
    assert discovered_test_file == ui_test_file


def test_discover_ios_project_files_honors_relative_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ios_dir = tmp_path / "mobile" / "ios-app"
    (ios_dir / "Demo.xcodeproj").mkdir(parents=True)
    ui_test_file = ios_dir / "DemoUITests" / "DemoUITests.swift"
    ui_test_file.parent.mkdir(parents=True)
    ui_test_file.write_text("import XCTest\n", encoding="utf-8")
    monkeypatch.setenv("QA_DEMO_IOS_PROJECT_DIR", "mobile/ios-app")

    project_path, discovered_test_file = discover_ios_project_files(repo_dir=tmp_path)

    assert project_path == ios_dir / "Demo.xcodeproj"
    assert discovered_test_file == ui_test_file


def test_discover_ios_project_files_prefers_workspace_when_present(tmp_path: Path) -> None:
    ios_dir = tmp_path / "clients" / "ios"
    (ios_dir / "example.xcodeproj").mkdir(parents=True)
    (ios_dir / "example.xcworkspace").mkdir()
    ui_test_file = ios_dir / "exampleUITests" / "exampleUITests.swift"
    ui_test_file.parent.mkdir(parents=True)
    ui_test_file.write_text("import XCTest\n", encoding="utf-8")

    project_path, discovered_test_file = discover_ios_project_files(
        repo_dir=tmp_path,
        target_source_paths=["clients/ios"],
    )

    assert project_path == ios_dir / "example.xcworkspace"
    assert discovered_test_file == ui_test_file


def test_ios_recorder_writes_failure_evidence_when_app_does_not_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    project_path = repo_dir / "example.xcodeproj"
    project_path.mkdir()
    ui_test_file = repo_dir / "exampleUITests" / "exampleUITests.swift"
    ui_test_file.parent.mkdir()
    ui_test_file.write_text("import XCTest\n", encoding="utf-8")
    xctestrun_path = tmp_path / "example.xctestrun"
    xctestrun_path.write_text("", encoding="utf-8")
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    output_dir = tmp_path / "videos"
    input_path.write_text(
        json.dumps(
            {
                "execution_repo_dir": str(repo_dir),
                "output_dir": str(output_dir),
                "capture_reference": "ios-simulator://configured",
                "scenarios": [
                    {
                        "name": "App load",
                        "objective": "Prove the app opens",
                        "capture_target": "ios",
                        "expected_outcomes": ["Ready screen appears"],
                        "steps": [{"action": "assert_visible", "selector": "text=Ready"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class _Recorder:
        returncode = None

        def __init__(self, args, **_kwargs):  # noqa: ANN001
            Path(args[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42" + (b"0" * 2048) + b"moov")

        def poll(self):  # noqa: ANN201
            return self.returncode

        def send_signal(self, _signal_value):  # noqa: ANN001
            return None

        def wait(self, timeout=None):  # noqa: ANN001, ANN201
            self.returncode = -signal.SIGINT
            return self.returncode

        def kill(self):  # noqa: ANN201
            self.returncode = -signal.SIGINT

    def _fake_run(args, **_kwargs):  # noqa: ANN001
        if "test-without-building" in args:
            raise RuntimeError("XCTAssert failed: Ready was not visible")
        if "xcresulttool" in args:
            return type(
                "Result",
                (),
                {
                    "stdout": json.dumps(
                        {
                            "issues": {
                                "testFailureSummaries": [
                                    {
                                        "testCaseName": "App load",
                                        "message": "App crashed before Ready screen",
                                    }
                                ]
                            }
                        }
                    ),
                    "stderr": "",
                    "returncode": 0,
                },
            )()
        return type("Result", (), {"stdout": "", "stderr": "", "returncode": 0})()

    monkeypatch.setenv("QA_DEMO_IOS_BUNDLE_ID", "com.example.app")
    monkeypatch.setenv("QA_DEMO_IOS_SIMULATOR_UDID", "SIM-123")
    monkeypatch.setattr("scripts.qa_demo_mobile_recorder.discover_ios_project_files", lambda **_kwargs: (project_path, ui_test_file))
    monkeypatch.setattr("scripts.qa_demo_mobile_recorder._reboot_simulator", lambda _udid: None)
    monkeypatch.setattr("scripts.qa_demo_mobile_recorder._build_for_testing", lambda **_kwargs: None)
    monkeypatch.setattr("scripts.qa_demo_mobile_recorder._discover_xctestrun_path", lambda _derived_data_dir: xctestrun_path)
    monkeypatch.setattr("scripts.qa_demo_mobile_recorder._reset_app_state", lambda **_kwargs: None)
    monkeypatch.setattr("scripts.qa_demo_mobile_recorder._run", _fake_run)
    monkeypatch.setattr("scripts.qa_demo_mobile_recorder.subprocess.Popen", _Recorder)

    result = main(["qa_demo_mobile_recorder.py", str(input_path), str(output_path)])

    output = json.loads(output_path.read_text(encoding="utf-8"))
    assert result == 1
    assert output["recordings"] == []
    assert output["failure_evidence"][0]["name"] == "App load"
    assert output["failure_evidence"][0]["capture_target"] == "ios"
    assert output["failure_evidence"][0]["capture_reference"] == "ios-simulator://configured"
    assert "Ready was not visible" in output["failure_evidence"][0]["error_message"]
    assert "iOS diagnostics:" in output["failure_evidence"][0]["error_message"]
    assert "App crashed before Ready screen" in output["failure_evidence"][0]["error_message"]
    assert Path(output["failure_evidence"][0]["path"]).exists()


def test_ios_recorder_writes_text_diagnostics_when_failure_video_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    project_path = repo_dir / "example.xcodeproj"
    project_path.mkdir()
    ui_test_file = repo_dir / "exampleUITests" / "exampleUITests.swift"
    ui_test_file.parent.mkdir()
    ui_test_file.write_text("import XCTest\n", encoding="utf-8")
    xctestrun_path = tmp_path / "example.xctestrun"
    xctestrun_path.write_text("", encoding="utf-8")
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    output_dir = tmp_path / "videos"
    input_path.write_text(
        json.dumps(
            {
                "execution_repo_dir": str(repo_dir),
                "output_dir": str(output_dir),
                "capture_reference": "ios-simulator://configured",
                "scenarios": [
                    {
                        "name": "App load",
                        "objective": "Prove the app opens",
                        "capture_target": "ios",
                        "expected_outcomes": ["Ready screen appears"],
                        "steps": [{"action": "assert_visible", "selector": "text=Ready"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class _Recorder:
        returncode = None

        def __init__(self, _args, **_kwargs):  # noqa: ANN001
            pass

        def poll(self):  # noqa: ANN201
            return self.returncode

        def send_signal(self, _signal_value):  # noqa: ANN001
            return None

        def wait(self, timeout=None):  # noqa: ANN001, ANN201
            self.returncode = -signal.SIGINT
            return self.returncode

        def kill(self):  # noqa: ANN201
            self.returncode = -signal.SIGINT

    def _fake_run(args, **_kwargs):  # noqa: ANN001
        if "test-without-building" in args:
            raise RuntimeError("XCTAssert failed: Ready was not visible")
        if "xcresulttool" in args:
            return type(
                "Result",
                (),
                {
                    "stdout": json.dumps(
                        {
                            "issues": {
                                "testFailureSummaries": [
                                    {
                                        "testCaseName": "App load",
                                        "message": "App crashed before Ready screen",
                                    }
                                ]
                            }
                        }
                    ),
                    "stderr": "",
                    "returncode": 0,
                },
            )()
        return type("Result", (), {"stdout": "", "stderr": "", "returncode": 0})()

    monkeypatch.setenv("QA_DEMO_IOS_BUNDLE_ID", "com.example.app")
    monkeypatch.setenv("QA_DEMO_IOS_SIMULATOR_UDID", "SIM-123")
    monkeypatch.setattr("scripts.qa_demo_mobile_recorder.discover_ios_project_files", lambda **_kwargs: (project_path, ui_test_file))
    monkeypatch.setattr("scripts.qa_demo_mobile_recorder._reboot_simulator", lambda _udid: None)
    monkeypatch.setattr("scripts.qa_demo_mobile_recorder._build_for_testing", lambda **_kwargs: None)
    monkeypatch.setattr("scripts.qa_demo_mobile_recorder._discover_xctestrun_path", lambda _derived_data_dir: xctestrun_path)
    monkeypatch.setattr("scripts.qa_demo_mobile_recorder._reset_app_state", lambda **_kwargs: None)
    monkeypatch.setattr("scripts.qa_demo_mobile_recorder._run", _fake_run)
    monkeypatch.setattr("scripts.qa_demo_mobile_recorder.subprocess.Popen", _Recorder)

    result = main(["qa_demo_mobile_recorder.py", str(input_path), str(output_path)])

    output = json.loads(output_path.read_text(encoding="utf-8"))
    failure_path = Path(output["failure_evidence"][0]["path"])
    assert result == 1
    assert failure_path.suffix == ".txt"
    assert failure_path.is_file()
    assert "Ready was not visible" in output["failure_evidence"][0]["error_message"]
    assert "App crashed before Ready screen" in output["failure_evidence"][0]["error_message"]
    assert "screen-recording-error:" in failure_path.read_text(encoding="utf-8")


def test_build_for_testing_uses_workspace_flag_for_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    workspace_path = tmp_path / "example.xcworkspace"
    workspace_path.mkdir()

    def _fake_run(args, *, cwd=None, **_kwargs):  # noqa: ANN001
        captured["args"] = args
        captured["cwd"] = cwd

        class _Result:
            stdout = ""

        return _Result()

    monkeypatch.setattr("scripts.qa_demo_mobile_recorder._run", _fake_run)

    _build_for_testing(
        project_path=workspace_path,
        scheme="example",
        simulator_udid="SIM-123",
        derived_data_dir=tmp_path / "DerivedData",
    )

    assert captured["args"][:3] == ["xcodebuild", "-workspace", str(workspace_path)]
    assert captured["cwd"] == workspace_path.parent
