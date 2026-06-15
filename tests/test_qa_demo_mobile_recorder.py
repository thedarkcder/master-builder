from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.qa_demo_mobile_recorder import _ios_command_timeout_seconds
from scripts.qa_demo_mobile_recorder import _combined_recording_failure
from scripts.qa_demo_mobile_recorder import _reboot_simulator
from scripts.qa_demo_mobile_recorder import _run
from scripts.qa_demo_mobile_recorder import discover_ios_project_files


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
