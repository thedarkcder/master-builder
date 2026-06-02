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
    discover_xcode_project,
    discover_xcuitest_file,
    generated_test_method_name,
    preferred_simulator_udid,
    render_xcuitest_source,
    simulator_test_build_flags,
)
from orchestrator.core.workflow.checkpoint_codec import decode_qa_result_payload  # noqa: E402


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

    project_path = discover_xcode_project(repo_dir=repo_dir)
    ui_test_file = discover_xcuitest_file(repo_dir=repo_dir)
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
    generated_source = render_xcuitest_source(test_class_name=test_class_name, scenarios=qa_result.scenarios)
    ui_test_file.write_text(generated_source, encoding="utf-8")

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
                raise combined_error
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


def resolve_bundle_identifier(*, project_path: Path, scheme: str) -> str:
    result = _run(
        ["xcodebuild", "-project", str(project_path), "-scheme", scheme, "-showBuildSettings"],
        capture_output=True,
    )
    match = re.search(r"PRODUCT_BUNDLE_IDENTIFIER = ([^\s]+)", result.stdout)
    if match is None:
        raise RuntimeError("Unable to resolve iOS bundle identifier from build settings")
    return match.group(1).strip()


def _ensure_simulator_booted(simulator_udid: str) -> None:
    subprocess.run(["xcrun", "simctl", "boot", simulator_udid], check=False, capture_output=True, text=True)
    _run(["xcrun", "simctl", "bootstatus", simulator_udid, "-b"], capture_output=True)


def _reboot_simulator(simulator_udid: str) -> None:
    subprocess.run(["xcrun", "simctl", "shutdown", simulator_udid], check=False, capture_output=True, text=True)
    _ensure_simulator_booted(simulator_udid)


def _build_for_testing(*, project_path: Path, scheme: str, simulator_udid: str, derived_data_dir: Path) -> None:
    _run(
        [
            "xcodebuild",
            "-project",
            str(project_path),
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


def _discover_xctestrun_path(derived_data_dir: Path) -> Path:
    candidates = sorted((derived_data_dir / "Build" / "Products").glob("*.xctestrun"))
    if not candidates:
        raise RuntimeError(f"No .xctestrun file found under {derived_data_dir}")
    return candidates[0]


def _reset_app_state(*, simulator_udid: str, bundle_id: str) -> None:
    subprocess.run(["xcrun", "simctl", "terminate", simulator_udid, bundle_id], check=False, capture_output=True, text=True)
    subprocess.run(["xcrun", "simctl", "uninstall", simulator_udid, bundle_id], check=False, capture_output=True, text=True)


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
    completed = subprocess.run(
        args,
        cwd=str(cwd) if cwd is not None else None,
        check=False,
        text=True,
        capture_output=capture_output,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"Command failed ({completed.returncode}): {' '.join(args)}\n{message}")
    return completed


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
