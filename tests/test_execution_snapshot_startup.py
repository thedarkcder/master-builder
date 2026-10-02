from __future__ import annotations

from dataclasses import dataclass
import unittest
from unittest.mock import patch

from orchestrator.core.workflow.execution_snapshot_startup import (
    ExecutionSnapshotStartupReport,
    _repair_execution_snapshot_payload,
    _repair_legacy_test_artifact,
    ensure_execution_snapshot_startup_bootstrap,
    run_execution_snapshot_startup_bootstrap,
)
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot


@dataclass
class _FakeSession:
    calls: list[tuple[str, object]]

    def execute(self, statement, params=None):  # noqa: ANN001, ANN201
        self.calls.append((str(statement), params))
        return []

    def commit(self) -> None:
        self.calls.append(("commit", None))


class _FakeSessionContext:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __enter__(self) -> _FakeSession:
        return self._session

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN201
        return False


class _FakeSessionFactory:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self):  # noqa: ANN204
        return _FakeSessionContext(self._session)


def _ok_report() -> ExecutionSnapshotStartupReport:
    return ExecutionSnapshotStartupReport(
        scanned_runs=1,
        invalid_runs=0,
        scanned_checkpoints=1,
        invalid_checkpoints=0,
        repaired_runs=0,
        repaired_checkpoints=0,
        invalid_run_ids=(),
        invalid_checkpoint_ids=(),
    )


class ExecutionSnapshotStartupBootstrapTests(unittest.TestCase):
    def test_startup_bootstrap_skips_non_postgres(self) -> None:
        session = _FakeSession(calls=[])
        factory = _FakeSessionFactory(session)
        with patch(
            "orchestrator.core.workflow.execution_snapshot_startup.is_postgres_database_url",
            side_effect=lambda _: False,
        ):
            report = run_execution_snapshot_startup_bootstrap(
                session_factory=factory,  # type: ignore[arg-type]
                database_url="sqlite:///tmp/test.db",
                actor="api",
            )

        self.assertIsNone(report)
        self.assertEqual(session.calls, [])

    def test_startup_bootstrap_runs_migration_under_advisory_lock(self) -> None:
        session = _FakeSession(calls=[])
        factory = _FakeSessionFactory(session)
        with (
            patch(
                "orchestrator.core.workflow.execution_snapshot_startup.is_postgres_database_url",
                side_effect=lambda _: True,
            ),
            patch(
                "orchestrator.core.workflow.execution_snapshot_startup._validate_execution_snapshots",
                return_value=_ok_report(),
            ),
        ):
            report = run_execution_snapshot_startup_bootstrap(
                session_factory=factory,  # type: ignore[arg-type]
                database_url="postgresql://user:pass@localhost/db",
                actor="worker",
            )

        self.assertIsNotNone(report)
        self.assertTrue(any("pg_advisory_lock" in stmt for stmt, _ in session.calls))
        self.assertTrue(any("pg_advisory_unlock" in stmt for stmt, _ in session.calls))
        self.assertEqual(session.calls[-1][0], "commit")

    def test_startup_bootstrap_logs_and_returns_report_on_invalid_rows(self) -> None:
        invalid_report = ExecutionSnapshotStartupReport(
            scanned_runs=1,
            invalid_runs=1,
            scanned_checkpoints=1,
            invalid_checkpoints=0,
            repaired_runs=0,
            repaired_checkpoints=0,
            invalid_run_ids=("run-1",),
            invalid_checkpoint_ids=(),
        )
        session = _FakeSession(calls=[])
        factory = _FakeSessionFactory(session)
        with (
            patch(
                "orchestrator.core.workflow.execution_snapshot_startup.is_postgres_database_url",
                side_effect=lambda _: True,
            ),
            patch(
                "orchestrator.core.workflow.execution_snapshot_startup._validate_execution_snapshots",
                return_value=invalid_report,
            ),
        ):
            with self.assertLogs(
                "orchestrator.core.workflow.execution_snapshot_startup",
                level="ERROR",
            ) as captured:
                report = run_execution_snapshot_startup_bootstrap(
                    session_factory=factory,  # type: ignore[arg-type]
                    database_url="postgresql://user:pass@localhost/db",
                    actor="api",
                )

        self.assertEqual(report, invalid_report)
        self.assertTrue(
            any(
                "execution_snapshot_startup_bootstrap_invalid_rows" in line
                for line in captured.output
            )
        )
        self.assertEqual(session.calls[-1][0], "commit")

    def test_ensure_startup_bootstrap_passes_when_report_clean(self) -> None:
        session = _FakeSession(calls=[])
        factory = _FakeSessionFactory(session)
        with patch(
            "orchestrator.core.workflow.execution_snapshot_startup.run_execution_snapshot_startup_bootstrap",
            return_value=_ok_report(),
        ):
            ensure_execution_snapshot_startup_bootstrap(
                session_factory=factory,  # type: ignore[arg-type]
                database_url="postgresql://user:pass@localhost/db",
                actor="api",
            )

    def test_ensure_startup_bootstrap_raises_on_invalid_rows(self) -> None:
        session = _FakeSession(calls=[])
        factory = _FakeSessionFactory(session)
        invalid_report = ExecutionSnapshotStartupReport(
            scanned_runs=1,
            invalid_runs=1,
            scanned_checkpoints=1,
            invalid_checkpoints=1,
            repaired_runs=0,
            repaired_checkpoints=0,
            invalid_run_ids=("run-1",),
            invalid_checkpoint_ids=("cp-1",),
        )
        with patch(
            "orchestrator.core.workflow.execution_snapshot_startup.run_execution_snapshot_startup_bootstrap",
            return_value=invalid_report,
        ):
            with self.assertRaises(RuntimeError):
                ensure_execution_snapshot_startup_bootstrap(
                    session_factory=factory,  # type: ignore[arg-type]
                    database_url="postgresql://user:pass@localhost/db",
                    actor="worker",
                )

    def test_repair_legacy_test_artifact_backfills_validation_scope(self) -> None:
        repaired = _repair_legacy_test_artifact(
            {
                "guidance": ["pytest -q"],
                "outcome": "continue",
                "feedback": None,
            }
        )

        self.assertEqual(
            repaired,
            {
                "guidance": ["pytest -q"],
                "outcome": "continue",
                "feedback": None,
                "validation_scope": "targeted_only",
            },
        )

    def test_repair_execution_snapshot_payload_repairs_legacy_test_stage(self) -> None:
        payload = {
            "version": 1,
            "context": {"trigger_context": {}, "execution_context": {}},
            "workflow": {
                "outcome": None,
                "attempts": 0,
                "summary": [],
                "blocker_message": None,
                "requeue_target": None,
                "requeue_reason": None,
            },
            "events": {
                "stage_updates": [],
                "live_stage_updates": [],
                "stage_trace": [],
                "workstream_trace": [],
            },
            "stages": {
                "test": {
                    "attempt": 1,
                    "status": "completed",
                    "summary": "tests passed",
                    "completed_at": "2026-06-02T00:00:00+00:00",
                    "artifact": {
                        "guidance": ["pytest -q"],
                        "outcome": "continue",
                        "feedback": None,
                    },
                }
            },
        }

        repaired = _repair_execution_snapshot_payload(payload)

        assert repaired is not None
        self.assertEqual(
            repaired["stages"]["test"]["artifact"]["validation_scope"],
            "targeted_only",
        )

    def test_repair_execution_snapshot_payload_migrates_legacy_mobile_capture_targets_to_ios(
        self,
    ) -> None:
        payload = {
            "version": 1,
            "context": {"trigger_context": {}, "execution_context": {}},
            "workflow": {
                "outcome": None,
                "attempts": 0,
                "summary": [],
                "blocker_message": None,
                "requeue_target": None,
                "requeue_reason": None,
            },
            "events": {
                "stage_updates": [],
                "live_stage_updates": [],
                "stage_trace": [],
                "workstream_trace": [],
            },
            "stages": {
                "pm": {
                    "attempt": 1,
                    "status": "completed",
                    "summary": "planned",
                    "completed_at": "2026-06-02T00:00:00+00:00",
                    "artifact": {
                        "plan_steps": ["step"],
                        "acceptance_criteria": ["native app works"],
                        "risks": [],
                        "demo_requirements": [
                            {
                                "title": "Native walkthrough",
                                "acceptance_criterion": "native app works",
                                "capture_target": "mobile",
                                "variants": [
                                    "Invalid input is rejected",
                                    "Repeat action remains safe",
                                ],
                            }
                        ],
                        "outcome": "continue",
                        "next_stage": "dev",
                        "execution_worker_capability": "macos",
                    },
                },
                "qa": {
                    "attempt": 1,
                    "status": "completed",
                    "summary": "recorded",
                    "completed_at": "2026-06-02T00:00:00+00:00",
                    "artifact": {
                        "summary": ["recorded"],
                        "scenarios": [
                            {
                                "name": "Native walkthrough",
                                "objective": "show app",
                                "capture_target": "mobile",
                                "start_path": "/",
                                "expected_outcomes": [],
                                "steps": [
                                    {
                                        "action": "assert_visible",
                                        "selector": "text=Ready",
                                        "value": None,
                                    }
                                ],
                            }
                        ],
                        "recordings": [
                            {
                                "name": "Native walkthrough",
                                "artifact_url": "https://cdn.example/demo.mp4",
                                "object_key": "tenant/project/run/demo.mp4",
                                "capture_reference": "mobile://configured",
                                "capture_target": "mobile",
                                "content_sha256": f"{1:064x}",
                                "release_commit_sha": "b" * 40,
                                "release_context_sha256": f"{2:064x}",
                            }
                        ],
                        "outcome": "continue",
                    },
                },
            },
        }

        repaired = _repair_execution_snapshot_payload(payload)

        assert repaired is not None
        pm_requirement = repaired["stages"]["pm"]["artifact"]["demo_requirements"][0]
        qa_scenario = repaired["stages"]["qa"]["artifact"]["scenarios"][0]
        qa_recording = repaired["stages"]["qa"]["artifact"]["recordings"][0]
        self.assertEqual(pm_requirement["capture_target"], "ios")
        self.assertEqual(qa_scenario["capture_target"], "ios")
        self.assertEqual(qa_recording["capture_target"], "ios")
        self.assertEqual(
            qa_recording["capture_reference"], "ios-simulator://configured"
        )

    def test_repair_execution_snapshot_payload_ignores_non_test_stage_artifacts(
        self,
    ) -> None:
        payload = {
            "version": 1,
            "context": {"trigger_context": {}, "execution_context": {}},
            "workflow": {
                "outcome": None,
                "attempts": 0,
                "summary": [],
                "blocker_message": None,
                "requeue_target": None,
                "requeue_reason": None,
            },
            "events": {
                "stage_updates": [],
                "live_stage_updates": [],
                "stage_trace": [],
                "workstream_trace": [],
            },
            "stages": {
                "dev": {
                    "attempt": 1,
                    "status": "completed",
                    "summary": "implemented",
                    "completed_at": "2026-06-02T00:00:00+00:00",
                    "artifact": {
                        "change_summary": ["done"],
                        "outcome": "continue",
                    },
                }
            },
        }

        repaired = _repair_execution_snapshot_payload(payload)

        self.assertIsNone(repaired)

    def test_repair_execution_snapshot_payload_blocks_legacy_empty_demo_variants_without_backfill(
        self,
    ) -> None:
        payload = {
            "version": 1,
            "context": {"trigger_context": {}, "execution_context": {}},
            "workflow": {
                "outcome": None,
                "attempts": 0,
                "summary": [],
                "blocker_message": None,
                "requeue_target": None,
                "requeue_reason": None,
            },
            "events": {
                "stage_updates": [],
                "live_stage_updates": [],
                "stage_trace": [],
                "workstream_trace": [],
            },
            "stages": {
                "pm": {
                    "attempt": 1,
                    "status": "completed",
                    "summary": "planned",
                    "completed_at": "2026-06-02T00:00:00+00:00",
                    "artifact": {
                        "plan_steps": ["step"],
                        "acceptance_criteria": ["browser workflow works"],
                        "risks": [],
                        "resolved_prerequisites": [],
                        "unresolved_prerequisites": [],
                        "demo_requirements": [
                            {
                                "title": "Browser walkthrough",
                                "acceptance_criterion": "browser workflow works",
                                "capture_target": "browser",
                                "variants": [],
                            }
                        ],
                        "outcome": "continue",
                        "next_stage": "dev",
                        "execution_worker_capability": "linux",
                    },
                }
            },
        }

        repaired = _repair_execution_snapshot_payload(payload)

        assert repaired is not None
        pm_stage = repaired["stages"]["pm"]
        pm_artifact = pm_stage["artifact"]
        self.assertEqual(pm_stage["status"], "blocked")
        self.assertEqual(pm_artifact["outcome"], "blocked")
        self.assertEqual(pm_artifact["demo_requirements"], [])
        self.assertIn("Legacy PM demo requirements", pm_artifact["blocker_message"])
        self.assertIsNotNone(ExecutionSnapshot.load(repaired))

    def test_repair_execution_snapshot_payload_blocks_legacy_missing_demo_capture_target_without_backfill(
        self,
    ) -> None:
        payload = {
            "version": 1,
            "context": {"trigger_context": {}, "execution_context": {}},
            "workflow": {
                "outcome": None,
                "attempts": 0,
                "summary": [],
                "blocker_message": None,
                "requeue_target": None,
                "requeue_reason": None,
            },
            "events": {
                "stage_updates": [],
                "live_stage_updates": [],
                "stage_trace": [],
                "workstream_trace": [],
            },
            "stages": {
                "pm": {
                    "attempt": 1,
                    "status": "completed",
                    "summary": "planned",
                    "completed_at": "2026-06-02T00:00:00+00:00",
                    "artifact": {
                        "plan_steps": ["step"],
                        "acceptance_criteria": ["browser workflow works"],
                        "risks": [],
                        "resolved_prerequisites": [],
                        "unresolved_prerequisites": [],
                        "demo_requirements": [
                            {
                                "title": "Browser walkthrough",
                                "acceptance_criterion": "browser workflow works",
                                "variants": [
                                    "Invalid input is rejected",
                                    "Repeat action remains safe",
                                ],
                            }
                        ],
                        "outcome": "continue",
                        "next_stage": "dev",
                        "execution_worker_capability": "linux",
                    },
                }
            },
        }

        repaired = _repair_execution_snapshot_payload(payload)

        assert repaired is not None
        pm_stage = repaired["stages"]["pm"]
        pm_artifact = pm_stage["artifact"]
        self.assertEqual(pm_stage["status"], "blocked")
        self.assertEqual(pm_artifact["outcome"], "blocked")
        self.assertEqual(pm_artifact["demo_requirements"], [])
        self.assertIn("Legacy PM demo requirements", pm_artifact["blocker_message"])
        self.assertIsNotNone(ExecutionSnapshot.load(repaired))

    def test_repair_execution_snapshot_payload_blocks_legacy_qa_recordings_without_digests(
        self,
    ) -> None:
        payload = {
            "version": 1,
            "context": {"trigger_context": {}, "execution_context": {}},
            "workflow": {
                "outcome": None,
                "attempts": 0,
                "summary": [],
                "blocker_message": None,
                "requeue_target": None,
                "requeue_reason": None,
            },
            "events": {
                "stage_updates": [],
                "live_stage_updates": [],
                "stage_trace": [],
                "workstream_trace": [],
            },
            "stages": {
                "qa": {
                    "attempt": 1,
                    "status": "completed",
                    "summary": "recorded",
                    "completed_at": "2026-06-02T00:00:00+00:00",
                    "artifact": {
                        "summary": ["recorded"],
                        "scenarios": [
                            {
                                "name": "Browser walkthrough",
                                "objective": "show app",
                                "capture_target": "browser",
                                "start_path": "/",
                                "expected_outcomes": ["Ready"],
                                "steps": [
                                    {
                                        "action": "assert_visible",
                                        "selector": "text=Ready",
                                        "value": None,
                                    }
                                ],
                            }
                        ],
                        "recordings": [
                            {
                                "name": "Browser walkthrough",
                                "artifact_url": "https://cdn.example/demo.webm",
                                "object_key": "tenant/project/run/demo.webm",
                                "capture_reference": "https://preview.example",
                                "capture_target": "browser",
                            }
                        ],
                        "outcome": "continue",
                    },
                }
            },
        }

        repaired = _repair_execution_snapshot_payload(payload)

        assert repaired is not None
        qa_artifact = repaired["stages"]["qa"]["artifact"]
        self.assertEqual(qa_artifact["outcome"], "blocked")
        self.assertEqual(qa_artifact["recordings"], [])
        self.assertIn(
            "Legacy QA demo recordings predate SHA-256 proof and release context metadata",
            qa_artifact["blocker_message"],
        )
        self.assertIsNotNone(ExecutionSnapshot.load(repaired))

    def test_repair_execution_snapshot_payload_backfills_legacy_qa_scenario_capture_targets(
        self,
    ) -> None:
        payload = {
            "version": 1,
            "context": {"trigger_context": {}, "execution_context": {}},
            "workflow": {
                "outcome": None,
                "attempts": 0,
                "summary": [],
                "blocker_message": None,
                "requeue_target": None,
                "requeue_reason": None,
            },
            "events": {
                "stage_updates": [],
                "live_stage_updates": [],
                "stage_trace": [],
                "workstream_trace": [],
            },
            "stages": {
                "qa": {
                    "attempt": 1,
                    "status": "completed",
                    "summary": "recorded",
                    "completed_at": "2026-06-02T00:00:00+00:00",
                    "artifact": {
                        "summary": ["recorded"],
                        "scenarios": [
                            {
                                "name": "Browser walkthrough",
                                "objective": "show app",
                                "start_path": "/",
                                "expected_outcomes": ["Ready"],
                                "steps": [
                                    {
                                        "action": "assert_visible",
                                        "selector": "text=Ready",
                                        "value": None,
                                    }
                                ],
                            }
                        ],
                        "recordings": [],
                        "outcome": "blocked",
                        "blocker_message": "Recording failed",
                    },
                }
            },
        }

        repaired = _repair_execution_snapshot_payload(payload)

        assert repaired is not None
        scenario = repaired["stages"]["qa"]["artifact"]["scenarios"][0]
        self.assertEqual(scenario["capture_target"], "browser")
        self.assertIsNotNone(ExecutionSnapshot.load(repaired))

    def test_execution_snapshot_load_rejects_invalid_qa_stage_artifact(self) -> None:
        payload = {
            "version": 1,
            "context": {"trigger_context": {}, "execution_context": {}},
            "workflow": {
                "outcome": None,
                "attempts": 0,
                "summary": [],
                "blocker_message": None,
                "requeue_target": None,
                "requeue_reason": None,
            },
            "events": {
                "stage_updates": [],
                "live_stage_updates": [],
                "stage_trace": [],
                "workstream_trace": [],
            },
            "stages": {
                "qa": {
                    "attempt": 1,
                    "status": "completed",
                    "summary": "recorded",
                    "completed_at": "2026-06-02T00:00:00+00:00",
                    "artifact": {
                        "summary": ["recorded"],
                        "scenarios": [],
                        "recordings": [
                            {
                                "name": "Browser walkthrough",
                                "artifact_url": "https://cdn.example/demo.webm",
                                "object_key": "tenant/project/run/demo.webm",
                                "capture_reference": "https://preview.example",
                                "capture_target": "browser",
                            }
                        ],
                        "outcome": "continue",
                    },
                }
            },
        }

        self.assertIsNone(ExecutionSnapshot.load(payload))


if __name__ == "__main__":
    unittest.main()
