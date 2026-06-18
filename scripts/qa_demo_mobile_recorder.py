#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.core.qa.mobile_xcuitest_recorder import (  # noqa: E402
    discover_xcuitest_file,
    generated_test_method_name,
    preferred_simulator_udid,
    render_xcuitest_source,
    simulator_test_build_flags,
)
from orchestrator.core.workflow.checkpoint_codec import decode_qa_result_payload  # noqa: E402
from scripts.qa_demo_release_context import qa_demo_launch_context  # noqa: E402

DEFAULT_IOS_COMMAND_TIMEOUT_SECONDS = 600


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        raise RuntimeError("usage: qa_demo_mobile_recorder.py <input.json> <output.json>")

    input_path = Path(argv[1]).resolve()
    output_path = Path(argv[2]).resolve()
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    repo_dir = Path(str(payload.get("execution_repo_dir") or "").strip()).resolve()
    if not repo_dir.exists():
        raise RuntimeError(f"execution_repo_dir does not exist: {repo_dir}")

    qa_result = decode_qa_result_payload(
        {
            "summary": ["mobile qa recorder"],
            "scenarios": payload.get("scenarios"),
            "recordings": [],
            "outcome": "continue",
        }
    )
    if qa_result is None or not qa_result.scenarios:
        raise RuntimeError("mobile qa recorder requires at least one valid scenario")

    target_source_paths = _target_source_paths(payload.get("target_source_paths"))
    project_path, ui_test_file = discover_ios_project_files(
        repo_dir=repo_dir,
        target_source_paths=target_source_paths,
    )
    test_class_name = ui_test_file.stem
    ui_test_target = ui_test_file.parent.name
    scheme = str(os.environ.get("QA_DEMO_IOS_SCHEME") or project_path.stem).strip()
    bundle_id = (
        str(os.environ.get("QA_DEMO_IOS_BUNDLE_ID") or "").strip()
        or resolve_bundle_identifier(project_path=project_path, scheme=scheme)
    )
    simulator_udid = (
        str(os.environ.get("QA_DEMO_IOS_SIMULATOR_UDID") or "").strip()
        or preferred_simulator_udid(
            _run(
                ["xcrun", "simctl", "list", "devices", "available"],
                capture_output=True,
            ).stdout
        )
    )

    output_dir = Path(str(payload.get("output_dir") or "").strip()).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    derived_data_dir = output_dir / "DerivedData"
    original_test_source = ui_test_file.read_text(encoding="utf-8")
    generated_source = render_xcuitest_source(
        test_class_name=test_class_name,
        scenarios=qa_result.scenarios,
        launch_environment=qa_demo_launch_environment(payload),
    )
    ui_test_file.write_text(generated_source, encoding="utf-8")
    capture_reference = str(payload.get("capture_reference") or "ios-simulator://configured").strip()

    try:
        _reboot_simulator(simulator_udid)
        _build_for_testing(
            project_path=project_path,
            scheme=scheme,
            simulator_udid=simulator_udid,
            derived_data_dir=derived_data_dir,
        )
        xctestrun_path = _discover_xctestrun_path(derived_data_dir)

        recordings: list[dict[str, str]] = []
        for scenario in qa_result.scenarios:
            sanitized_name = sanitize_recording_name(scenario.name)
            video_path = output_dir / f"{sanitized_name}.mp4"
            result_bundle_path = output_dir / "result-bundles" / f"{sanitized_name}.xcresult"
            result_bundle_path.parent.mkdir(parents=True, exist_ok=True)
            _reset_app_state(simulator_udid=simulator_udid, bundle_id=bundle_id)
            recorder = subprocess.Popen(
                ["xcrun", "simctl", "io", simulator_udid, "recordVideo", "--force", str(video_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            primary_error: Exception | None = None
            stop_error: Exception | None = None
            try:
                _run(
                    [
                        "xcodebuild",
                        "test-without-building",
                        "-xctestrun",
                        str(xctestrun_path),
                        "-destination",
                        f"id={simulator_udid}",
                        f"-only-testing:{ui_test_target}/{test_class_name}/{generated_test_method_name(scenario.name)}",
                        "-resultBundlePath",
                        str(result_bundle_path),
                    ],
                    cwd=repo_dir,
                    capture_output=True,
                )
            except Exception as exc:  # pragma: no cover - exercised via helper tests
                primary_error = RuntimeError(f"{exc}\nResult bundle: {result_bundle_path}")
            finally:
                try:
                    _stop_video_recording(recorder)
                except Exception as exc:  # pragma: no cover - exercised via helper tests
                    stop_error = exc
            combined_error = _combined_recording_failure(primary_error=primary_error, stop_error=stop_error)
            if combined_error is not None:
                diagnostics = _ios_failure_diagnostics(result_bundle_path=result_bundle_path)
                failure_path = video_path
                try:
                    _validate_mp4_recording(video_path)
                except Exception as recording_exc:  # noqa: BLE001
                    diagnostics = "\n".join(
                        part
                        for part in (
                            diagnostics,
                            f"screen-recording-error: {recording_exc}",
                        )
                        if part
                    )
                    failure_path = output_dir / f"{sanitized_name}-failure.txt"
                    failure_path.write_text(
                        _ios_failure_message(
                            scenario_name=scenario.name,
                            error=combined_error,
                            diagnostics=diagnostics,
                        ),
                        encoding="utf-8",
                    )
                output_path.write_text(
                    json.dumps(
                        {
                            "recordings": recordings,
                            "failure_evidence": [
                                {
                                    "name": scenario.name,
                                    "path": str(failure_path),
                                    "error_message": _ios_failure_message(
                                        scenario_name=scenario.name,
                                        error=combined_error,
                                        diagnostics=diagnostics,
                                    ),
                                    "capture_target": "ios",
                                    "capture_reference": capture_reference,
                                }
                            ],
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                return 1
            if not video_path.exists():
                raise RuntimeError(f"Expected mobile QA recording missing: {video_path}")
            recordings.append({"name": scenario.name, "path": str(video_path)})

        output_path.write_text(json.dumps({"recordings": recordings}, indent=2), encoding="utf-8")
        return 0
    finally:
        ui_test_file.write_text(original_test_source, encoding="utf-8")


def sanitize_recording_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(name or "demo").strip().lower()).strip("-")
    return normalized or "demo"


def _ios_failure_message(*, scenario_name: str, error: Exception, diagnostics: str) -> str:
    parts = [f"QA demo iOS scenario failed: {scenario_name}", str(error)]
    if diagnostics:
        parts.append(diagnostics)
    return "\n".join(parts)


def _ios_failure_diagnostics(*, result_bundle_path: Path) -> str:
    try:
        result = _run(
            [
                "xcrun",
                "xcresulttool",
                "get",
                "--legacy",
                "--path",
                str(result_bundle_path),
                "--format",
                "json",
            ],
            capture_output=True,
        )
    except Exception as exc:  # noqa: BLE001
        return f"iOS diagnostics unavailable: {exc}"
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return f"iOS diagnostics unavailable: xcresult payload was not valid JSON: {exc}"
    messages = _xcresult_failure_messages(payload)
    if not messages:
        return ""
    return "iOS diagnostics:\n" + _bounded_diagnostic_text(messages)


def _xcresult_failure_messages(value: object) -> list[str]:
    messages: list[str] = []

    def visit(node: object, *, key: str | None = None) -> None:
        if isinstance(node, dict):
            for child_key, child_value in node.items():
                visit(child_value, key=str(child_key))
            return
        if isinstance(node, list):
            for item in node:
                visit(item, key=key)
            return
        if key not in {"message", "testCaseName", "failureText", "issueType"}:
            return
        text = str(node or "").strip()
        if text:
            messages.append(text)

    visit(value)
    return list(dict.fromkeys(messages))[-40:]


def _bounded_diagnostic_text(lines: list[str], *, max_chars: int = 4000) -> str:
    text = "\n".join(lines).strip()
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _validate_mp4_recording(path: Path) -> None:
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"Expected iOS QA failure recording missing: {path}")
    payload = path.read_bytes()
    if len(payload) < 1024 or b"ftyp" not in payload[:64] or b"moov" not in payload:
        raise RuntimeError(f"iOS QA failure recording is not valid video evidence: {path}")


def _target_source_paths(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def qa_demo_launch_environment(payload: dict[str, object]) -> dict[str, str]:
    return qa_demo_launch_context(payload)


def discover_ios_project_files(
    *,
    repo_dir: Path,
    target_source_paths: list[str] | None = None,
) -> tuple[Path, Path]:
    search_roots = _ios_project_search_roots(
        repo_dir=repo_dir,
        target_source_paths=target_source_paths,
    )
    project_paths = sorted(
        project_path
        for search_root in search_roots
        for project_path in search_root.rglob("*.xcodeproj")
    )
    workspace_paths = sorted(
        workspace_path
        for search_root in search_roots
        for workspace_path in search_root.rglob("*.xcworkspace")
    )
    swift_files = sorted(
        swift_file
        for search_root in search_roots
        for swift_file in search_root.rglob("*UITests/*.swift")
    )
    return (
        _discover_xcode_container(repo_dir=repo_dir, workspace_paths=workspace_paths, project_paths=project_paths),
        discover_xcuitest_file(repo_dir=repo_dir, swift_files=swift_files),
    )


def _discover_xcode_container(*, repo_dir: Path, workspace_paths: list[Path], project_paths: list[Path]) -> Path:
    candidates = list(workspace_paths or project_paths)
    if not candidates:
        raise ValueError(f"No Xcode project or workspace found under {repo_dir}")
    root_candidates = [path for path in candidates if path.parent == repo_dir]
    preferred = root_candidates or candidates
    return sorted(preferred, key=lambda path: (len(path.parts), str(path)))[0]


def _ios_project_search_roots(*, repo_dir: Path, target_source_paths: list[str] | None = None) -> list[Path]:
    configured = str(os.environ.get("QA_DEMO_IOS_PROJECT_DIR") or "").strip()
    if configured:
        return [_resolve_repo_relative_source_path(repo_dir=repo_dir, source_path=configured)]
    roots = [
        _resolve_repo_relative_source_path(repo_dir=repo_dir, source_path=source_path)
        for source_path in target_source_paths or []
    ]
    return roots or [repo_dir]


def _resolve_repo_relative_source_path(*, repo_dir: Path, source_path: str) -> Path:
    raw_path = Path(str(source_path or "").strip())
    if raw_path.is_absolute():
        raise RuntimeError(f"iOS project source path must be repo-relative: {source_path}")
    candidate = (repo_dir / raw_path).resolve()
    try:
        candidate.relative_to(repo_dir.resolve())
    except ValueError as exc:
        raise RuntimeError(f"iOS project source path escapes the repository: {source_path}") from exc
    if not candidate.exists():
        raise RuntimeError(f"iOS project source path does not exist: {source_path}")
    return candidate


def resolve_bundle_identifier(*, project_path: Path, scheme: str) -> str:
    result = _run(
        ["xcodebuild", *_xcode_container_args(project_path), "-scheme", scheme, "-showBuildSettings"],
        capture_output=True,
    )
    match = re.search(r"PRODUCT_BUNDLE_IDENTIFIER = ([^\s]+)", result.stdout)
    if match is None:
        raise RuntimeError("Unable to resolve iOS bundle identifier from build settings")
    return match.group(1).strip()


def _ensure_simulator_booted(simulator_udid: str) -> None:
    _run_unchecked(["xcrun", "simctl", "boot", simulator_udid])
    _run(["xcrun", "simctl", "bootstatus", simulator_udid, "-b"], capture_output=True)


def _reboot_simulator(simulator_udid: str) -> None:
    _run_unchecked(["xcrun", "simctl", "shutdown", simulator_udid])
    _ensure_simulator_booted(simulator_udid)


def _build_for_testing(*, project_path: Path, scheme: str, simulator_udid: str, derived_data_dir: Path) -> None:
    _run(
        [
            "xcodebuild",
            *_xcode_container_args(project_path),
            "-scheme",
            scheme,
            "-destination",
            f"id={simulator_udid}",
            "-derivedDataPath",
            str(derived_data_dir),
            "build-for-testing",
            *simulator_test_build_flags(),
        ],
        cwd=project_path.parent,
        capture_output=True,
    )


def _xcode_container_args(project_path: Path) -> list[str]:
    suffix = project_path.suffix.lower()
    if suffix == ".xcworkspace":
        return ["-workspace", str(project_path)]
    if suffix == ".xcodeproj":
        return ["-project", str(project_path)]
    raise RuntimeError(f"Unsupported Xcode container type: {project_path}")


def _discover_xctestrun_path(derived_data_dir: Path) -> Path:
    candidates = sorted((derived_data_dir / "Build" / "Products").glob("*.xctestrun"))
    if not candidates:
        raise RuntimeError(f"No .xctestrun file found under {derived_data_dir}")
    return candidates[0]


def _reset_app_state(*, simulator_udid: str, bundle_id: str) -> None:
    _run_unchecked(["xcrun", "simctl", "terminate", simulator_udid, bundle_id])
    _run_unchecked(["xcrun", "simctl", "uninstall", simulator_udid, bundle_id])


def _stop_video_recording(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:  # pragma: no cover
            process.kill()
            process.wait(timeout=5)
            raise RuntimeError("Timed out stopping simulator video recording") from exc
    if process.returncode not in {0, -signal.SIGINT}:
        raise RuntimeError(f"Simulator video recording failed with exit code {process.returncode}")


def _combined_recording_failure(
    *,
    primary_error: Exception | None,
    stop_error: Exception | None,
) -> Exception | None:
    if primary_error is None:
        return stop_error
    if stop_error is None:
        return primary_error
    return RuntimeError(f"{primary_error}\nVideo recording shutdown also failed: {stop_error}")


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    capture_output: bool,
) -> subprocess.CompletedProcess[str]:
    timeout_seconds = _ios_command_timeout_seconds()
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd) if cwd is not None else None,
            check=False,
            text=True,
            capture_output=capture_output,
            timeout=timeout_seconds,
        )
    except (subprocess.TimeoutExpired, TimeoutError) as exc:
        raise _timeout_error(args=args, timeout_seconds=timeout_seconds) from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"Command failed ({completed.returncode}): {' '.join(args)}\n{message}")
    return completed


def _run_unchecked(args: list[str]) -> None:
    timeout_seconds = _ios_command_timeout_seconds()
    try:
        subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (subprocess.TimeoutExpired, TimeoutError) as exc:
        raise _timeout_error(args=args, timeout_seconds=timeout_seconds) from exc


def _timeout_error(*, args: list[str], timeout_seconds: int) -> RuntimeError:
    return RuntimeError(f"iOS QA recorder command timed out after {timeout_seconds} seconds: {' '.join(args)}")


def _ios_command_timeout_seconds() -> int:
    raw_value = str(os.environ.get("QA_DEMO_IOS_COMMAND_TIMEOUT_SECONDS") or "").strip()
    if not raw_value:
        return DEFAULT_IOS_COMMAND_TIMEOUT_SECONDS
    try:
        configured = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("QA_DEMO_IOS_COMMAND_TIMEOUT_SECONDS must be an integer") from exc
    if configured < 1:
        raise RuntimeError("QA_DEMO_IOS_COMMAND_TIMEOUT_SECONDS must be greater than zero")
    return configured


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
