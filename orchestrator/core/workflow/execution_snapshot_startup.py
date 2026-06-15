from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.checkpoint_codec import decode_pm_plan_payload
from orchestrator.storage.run_queue_events import is_postgres_database_url
from orchestrator.storage.models import Run, WorkflowCheckpoint

logger = logging.getLogger(__name__)

_EXECUTION_SNAPSHOT_MIGRATION_LOCK_KEY = 740_002_611
_LEGACY_QA_RECORDING_DIGEST_BLOCKER = (
    "Legacy QA demo recordings predate SHA-256 proof and release context metadata and must be regenerated."
)
_LEGACY_PM_DEMO_REQUIREMENT_BLOCKER = (
    "Legacy PM demo requirements do not satisfy the current QA demo proof contract and must be regenerated."
)


@dataclass(frozen=True)
class ExecutionSnapshotStartupReport:
    scanned_runs: int
    invalid_runs: int
    scanned_checkpoints: int
    invalid_checkpoints: int
    repaired_runs: int
    repaired_checkpoints: int
    invalid_run_ids: tuple[str, ...]
    invalid_checkpoint_ids: tuple[str, ...]


def run_execution_snapshot_startup_bootstrap(
    *,
    session_factory: sessionmaker[Session],
    database_url: str,
    actor: str,
) -> ExecutionSnapshotStartupReport | None:
    if not is_postgres_database_url(database_url):
        logger.info(
            "execution_snapshot_startup_bootstrap_skipped actor=%s reason=non_postgres",
            actor,
        )
        return None

    with session_factory() as session:
        session.execute(
            text("SELECT pg_advisory_lock(:lock_key)"),
            {"lock_key": _EXECUTION_SNAPSHOT_MIGRATION_LOCK_KEY},
        )
        try:
            repaired_runs, repaired_checkpoints = _repair_execution_snapshots(session=session)
            report = _validate_execution_snapshots(session=session)
        finally:
            session.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": _EXECUTION_SNAPSHOT_MIGRATION_LOCK_KEY},
            )
            session.commit()

    report = ExecutionSnapshotStartupReport(
        scanned_runs=report.scanned_runs,
        invalid_runs=report.invalid_runs,
        scanned_checkpoints=report.scanned_checkpoints,
        invalid_checkpoints=report.invalid_checkpoints,
        repaired_runs=repaired_runs,
        repaired_checkpoints=repaired_checkpoints,
        invalid_run_ids=report.invalid_run_ids,
        invalid_checkpoint_ids=report.invalid_checkpoint_ids,
    )

    if report.invalid_runs or report.invalid_checkpoints:
        logger.error(
            "execution_snapshot_startup_bootstrap_invalid_rows "
            "actor=%s invalid_runs=%s invalid_checkpoints=%s invalid_run_ids=%s invalid_checkpoint_ids=%s",
            actor,
            report.invalid_runs,
            report.invalid_checkpoints,
            list(report.invalid_run_ids),
            list(report.invalid_checkpoint_ids),
        )
        return report

    logger.info(
        "execution_snapshot_startup_bootstrap_valid actor=%s scanned_runs=%s scanned_checkpoints=%s "
        "repaired_runs=%s repaired_checkpoints=%s",
        actor,
        report.scanned_runs,
        report.scanned_checkpoints,
        report.repaired_runs,
        report.repaired_checkpoints,
    )
    return report


def ensure_execution_snapshot_startup_bootstrap(
    *,
    session_factory: sessionmaker[Session],
    database_url: str,
    actor: str,
) -> None:
    report = run_execution_snapshot_startup_bootstrap(
        session_factory=session_factory,
        database_url=database_url,
        actor=actor,
    )
    if report is None:
        return
    if report.invalid_runs or report.invalid_checkpoints:
        raise RuntimeError(
            "Execution snapshot startup validation found incompatible rows; "
            f"invalid_runs={report.invalid_runs} invalid_checkpoints={report.invalid_checkpoints}"
        )


def _validate_execution_snapshots(*, session: Session) -> ExecutionSnapshotStartupReport:
    scanned_runs = 0
    invalid_runs = 0
    invalid_run_ids: list[str] = []

    run_statement = select(Run.run_id, Run.plan).order_by(Run.created_at.asc(), Run.run_id.asc())
    for run_id, payload in session.execute(run_statement):
        scanned_runs += 1
        if payload is None:
            continue
        if ExecutionSnapshot.load(payload) is None:
            invalid_runs += 1
            invalid_run_ids.append(str(run_id))

    scanned_checkpoints = 0
    invalid_checkpoints = 0
    invalid_checkpoint_ids: list[str] = []

    checkpoint_statement = select(
        WorkflowCheckpoint.checkpoint_id,
        WorkflowCheckpoint.payload_json,
    ).order_by(WorkflowCheckpoint.created_at.asc(), WorkflowCheckpoint.checkpoint_id.asc())
    for checkpoint_id, payload in session.execute(checkpoint_statement):
        scanned_checkpoints += 1
        if payload is None:
            invalid_checkpoints += 1
            invalid_checkpoint_ids.append(str(checkpoint_id))
            continue
        if ExecutionSnapshot.load(payload) is None:
            invalid_checkpoints += 1
            invalid_checkpoint_ids.append(str(checkpoint_id))

    return ExecutionSnapshotStartupReport(
        scanned_runs=scanned_runs,
        invalid_runs=invalid_runs,
        scanned_checkpoints=scanned_checkpoints,
        invalid_checkpoints=invalid_checkpoints,
        repaired_runs=0,
        repaired_checkpoints=0,
        invalid_run_ids=tuple(invalid_run_ids),
        invalid_checkpoint_ids=tuple(invalid_checkpoint_ids),
    )


def _repair_execution_snapshots(*, session: Session) -> tuple[int, int]:
    run_updates: list[dict[str, object]] = []
    run_statement = select(Run.run_id, Run.plan).order_by(Run.created_at.asc(), Run.run_id.asc())
    for run_id, payload in session.execute(run_statement):
        repaired = _repair_execution_snapshot_payload(payload)
        if repaired is None:
            continue
        run_updates.append({"run_id": run_id, "plan": repaired})
    for update in run_updates:
        session.execute(
            Run.__table__.update().where(Run.run_id == update["run_id"]).values(plan=update["plan"]),
        )

    checkpoint_updates: list[dict[str, object]] = []
    checkpoint_statement = select(
        WorkflowCheckpoint.checkpoint_id,
        WorkflowCheckpoint.payload_json,
    ).order_by(WorkflowCheckpoint.created_at.asc(), WorkflowCheckpoint.checkpoint_id.asc())
    for checkpoint_id, payload in session.execute(checkpoint_statement):
        repaired = _repair_execution_snapshot_payload(payload)
        if repaired is None:
            continue
        checkpoint_updates.append(
            {
                "checkpoint_id": checkpoint_id,
                "payload_json": repaired,
                "updated_at": datetime.now(timezone.utc),
            }
        )
    for update in checkpoint_updates:
        session.execute(
            WorkflowCheckpoint.__table__.update()
            .where(WorkflowCheckpoint.checkpoint_id == update["checkpoint_id"])
            .values(payload_json=update["payload_json"], updated_at=update["updated_at"]),
        )
    return len(run_updates), len(checkpoint_updates)


def _repair_execution_snapshot_payload(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    stages = payload.get("stages")
    if not isinstance(stages, dict):
        return None

    repaired_stages: dict[str, Any] | None = None
    for stage_name, stage_record in stages.items():
        repaired_stage_record = _repair_execution_stage_record(stage_name=stage_name, stage_record=stage_record)
        if repaired_stage_record is None:
            continue
        if repaired_stages is None:
            repaired_stages = dict(stages)
        repaired_stages[stage_name] = repaired_stage_record

    if repaired_stages is None:
        return None

    repaired_payload = dict(payload)
    repaired_payload["stages"] = repaired_stages
    return repaired_payload


def _repair_execution_stage_record(*, stage_name: object, stage_record: object) -> dict[str, Any] | None:
    if not isinstance(stage_record, dict):
        return None
    artifact = stage_record.get("artifact")
    repaired_artifact = _repair_legacy_pm_demo_requirements(artifact) if stage_name == "pm" else None
    test_repaired_artifact = _repair_legacy_test_artifact(artifact) if stage_name == "test" else None
    if test_repaired_artifact is not None:
        repaired_artifact = (
            test_repaired_artifact
            if repaired_artifact is None
            else _merge_artifact_repairs(repaired_artifact, test_repaired_artifact)
        )
    qa_digest_repaired_artifact = (
        _repair_legacy_qa_recordings_without_content_sha256(artifact) if stage_name == "qa" else None
    )
    if qa_digest_repaired_artifact is not None:
        repaired_artifact = (
            qa_digest_repaired_artifact
            if repaired_artifact is None
            else _merge_artifact_repairs(repaired_artifact, qa_digest_repaired_artifact)
        )
    qa_scenario_repaired_artifact = _repair_legacy_qa_scenario_capture_targets(artifact) if stage_name == "qa" else None
    if qa_scenario_repaired_artifact is not None:
        repaired_artifact = (
            qa_scenario_repaired_artifact
            if repaired_artifact is None
            else _merge_artifact_repairs(repaired_artifact, qa_scenario_repaired_artifact)
        )
    capture_target_repaired_artifact = _repair_legacy_mobile_capture_targets(artifact)
    if capture_target_repaired_artifact is not None:
        repaired_artifact = capture_target_repaired_artifact if repaired_artifact is None else _merge_artifact_repairs(
            repaired_artifact,
            capture_target_repaired_artifact,
        )
    if repaired_artifact is None:
        return None
    repaired_stage_record = dict(stage_record)
    repaired_stage_record["artifact"] = repaired_artifact
    if stage_name == "pm" and repaired_artifact.get("outcome") == "blocked":
        repaired_stage_record["status"] = "blocked"
        repaired_stage_record["summary"] = repaired_artifact.get("blocker_message") or _LEGACY_PM_DEMO_REQUIREMENT_BLOCKER
    return repaired_stage_record


def _repair_legacy_pm_demo_requirements(artifact: object) -> dict[str, Any] | None:
    if not isinstance(artifact, dict):
        return None
    if decode_pm_plan_payload(artifact) is not None:
        return None
    demo_requirements = artifact.get("demo_requirements")
    if not isinstance(demo_requirements, list) or not demo_requirements:
        return None
    if not _pm_plan_core_fields_are_repairable(artifact):
        return None
    invalid_titles = _invalid_legacy_demo_requirement_titles(demo_requirements)
    if not invalid_titles:
        return None

    repaired_artifact = dict(artifact)
    repaired_artifact["demo_requirements"] = []
    repaired_artifact["outcome"] = "blocked"
    repaired_artifact["blocker_message"] = (
        _LEGACY_PM_DEMO_REQUIREMENT_BLOCKER
        + " Invalid requirement(s): "
        + "; ".join(invalid_titles)
    )
    repaired_artifact["requeue_target"] = None
    repaired_artifact["requeue_reason"] = None
    return repaired_artifact


def _pm_plan_core_fields_are_repairable(artifact: dict[str, Any]) -> bool:
    if _parse_string_list(artifact.get("plan_steps"), require_non_empty=True) is None:
        return False
    if _parse_string_list(artifact.get("acceptance_criteria"), require_non_empty=True) is None:
        return False
    if _parse_string_list(artifact.get("risks", []), require_non_empty=False) is None:
        return False
    if _parse_string_list(artifact.get("resolved_prerequisites", []), require_non_empty=False) is None:
        return False
    if _parse_string_list(artifact.get("unresolved_prerequisites", []), require_non_empty=False) is None:
        return False
    if artifact.get("next_stage") not in {"dev", "test"}:
        return False
    if artifact.get("execution_worker_capability") not in {"linux", "macos"}:
        return False
    return True


def _parse_string_list(value: object, *, require_non_empty: bool) -> list[str] | None:
    if not isinstance(value, list):
        return None
    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        if not item.strip():
            return None
        parsed.append(item)
    if require_non_empty and not parsed:
        return None
    return parsed


def _invalid_legacy_demo_requirement_titles(demo_requirements: list[object]) -> list[str]:
    invalid_titles: list[str] = []
    for index, requirement in enumerate(demo_requirements, start=1):
        if not isinstance(requirement, dict):
            invalid_titles.append(f"requirement {index}")
            continue
        title = str(requirement.get("title") or f"requirement {index}").strip() or f"requirement {index}"
        acceptance_criterion = str(requirement.get("acceptance_criterion") or "").strip()
        capture_target = str(requirement.get("capture_target") or "").strip()
        variants = requirement.get("variants")
        if (
            not title
            or not acceptance_criterion
            or capture_target not in {"browser", "ios", "android", "desktop"}
            or _parse_string_list(variants, require_non_empty=True) is None
            or len(variants) < 2
        ):
            invalid_titles.append(title)
    return invalid_titles


def _repair_legacy_test_artifact(artifact: object) -> dict[str, Any] | None:
    if not isinstance(artifact, dict):
        return None
    if artifact.get("validation_scope") is not None:
        return None
    guidance = artifact.get("guidance")
    outcome = artifact.get("outcome")
    if not isinstance(guidance, list) or not guidance or not all(isinstance(item, str) and item.strip() for item in guidance):
        return None
    if not isinstance(outcome, str) or not outcome.strip():
        return None
    repaired_artifact = dict(artifact)
    repaired_artifact["validation_scope"] = "targeted_only"
    return repaired_artifact


def _merge_artifact_repairs(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    merged = dict(primary)
    for key, value in secondary.items():
        merged[key] = value
    return merged


def _repair_legacy_qa_recordings_without_content_sha256(artifact: object) -> dict[str, Any] | None:
    if not isinstance(artifact, dict):
        return None
    recordings = artifact.get("recordings")
    if not isinstance(recordings, list) or not recordings:
        return None
    if any(not isinstance(recording, dict) for recording in recordings):
        return None
    if all(
        _is_valid_content_sha256(recording.get("content_sha256"))
        and _is_valid_commit_sha(recording.get("release_commit_sha"))
        and _is_valid_content_sha256(recording.get("release_context_sha256"))
        for recording in recordings
    ):
        return None

    summary = artifact.get("summary")
    if not isinstance(summary, list) or not all(isinstance(item, str) and item.strip() for item in summary):
        return None
    repaired_summary = list(summary)
    if _LEGACY_QA_RECORDING_DIGEST_BLOCKER not in repaired_summary:
        repaired_summary.append(_LEGACY_QA_RECORDING_DIGEST_BLOCKER)

    repaired_artifact = dict(artifact)
    repaired_artifact["summary"] = repaired_summary
    repaired_artifact["recordings"] = []
    repaired_artifact["outcome"] = "blocked"
    repaired_artifact["blocker_message"] = _LEGACY_QA_RECORDING_DIGEST_BLOCKER
    return repaired_artifact


def _repair_legacy_qa_scenario_capture_targets(artifact: object) -> dict[str, Any] | None:
    if not isinstance(artifact, dict):
        return None
    scenarios = artifact.get("scenarios")
    if not isinstance(scenarios, list):
        return None
    repaired_scenarios: list[Any] = []
    changed = False
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            return None
        repaired_scenario = dict(scenario)
        if "capture_target" not in repaired_scenario:
            repaired_scenario["capture_target"] = "browser"
            changed = True
        repaired_scenarios.append(repaired_scenario)
    if not changed:
        return None
    repaired_artifact = dict(artifact)
    repaired_artifact["scenarios"] = repaired_scenarios
    return repaired_artifact


def _is_valid_content_sha256(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return len(normalized) == 64 and all(char in "0123456789abcdef" for char in normalized)


def _is_valid_commit_sha(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return 7 <= len(normalized) <= 64 and all(char in "0123456789abcdef" for char in normalized)


def _repair_legacy_mobile_capture_targets(artifact: object) -> dict[str, Any] | None:
    if not isinstance(artifact, dict):
        return None
    repaired = _repair_capture_targets_in_value(artifact)
    if repaired == artifact:
        return None
    if not isinstance(repaired, dict):  # pragma: no cover
        return None
    return repaired


def _repair_capture_targets_in_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_repair_capture_targets_in_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    repaired: dict[str, Any] = {}
    for key, item in value.items():
        if key == "capture_target" and item == "mobile":
            repaired[key] = "ios"
        elif key == "capture_reference" and item == "mobile://configured":
            repaired[key] = "ios-simulator://configured"
        else:
            repaired[key] = _repair_capture_targets_in_value(item)
    return repaired
