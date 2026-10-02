from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.core.decision.resolution_service import resolve_slots_with_runtime
from orchestrator.core.knowledge.base import SlotResolution


class _RuntimeErrorForTest(RuntimeError):
    pass


def test_resolve_slots_with_runtime_uses_issue_context_without_repo_evidence() -> None:
    invoke_mock = MagicMock(
        return_value={"answers": {"decision_owner": "Platform Security"}}
    )
    result = resolve_slots_with_runtime(
        session=MagicMock(),
        settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/nope"),
        tenant=SimpleNamespace(tenant_id="tenant-1"),
        project=SimpleNamespace(project_id="project-1"),
        issue_key="GP-122",
        issue_summary="Need auth decision owner",
        issue_description="Security owner is Platform Security.",
        missing_slots=["decision_owner"],
        build_runtime_fn=lambda **_kwargs: object(),
        invoke_runtime_json_fn=invoke_mock,
        project_repo_dir_fn=lambda **_kwargs: __import__("pathlib").Path(
            "/tmp/does-not-exist"
        ),
        runtime_error_type=_RuntimeErrorForTest,
    )

    assert isinstance(result["decision_owner"], SlotResolution)
    assert result["decision_owner"].slot_value == "Platform Security"
    invoke_mock.assert_called_once()


def test_resolve_slots_with_runtime_skips_when_no_repo_or_issue_context() -> None:
    invoke_mock = MagicMock()
    result = resolve_slots_with_runtime(
        session=MagicMock(),
        settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/nope"),
        tenant=SimpleNamespace(tenant_id="tenant-1"),
        project=SimpleNamespace(project_id="project-1"),
        issue_key="GP-122",
        issue_summary="",
        issue_description="",
        missing_slots=["decision_owner"],
        build_runtime_fn=lambda **_kwargs: object(),
        invoke_runtime_json_fn=invoke_mock,
        project_repo_dir_fn=lambda **_kwargs: __import__("pathlib").Path(
            "/tmp/does-not-exist"
        ),
        runtime_error_type=_RuntimeErrorForTest,
    )

    assert result == {}
    invoke_mock.assert_not_called()


def test_resolve_slots_with_runtime_fails_on_runtime_error() -> None:
    def _raise_runtime_error(**_kwargs):
        raise _RuntimeErrorForTest("worker task failed")

    with pytest.raises(RuntimeError, match="Runtime decision resolution failed"):
        resolve_slots_with_runtime(
            session=MagicMock(),
            settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/nope"),
            tenant=SimpleNamespace(tenant_id="tenant-1"),
            project=SimpleNamespace(project_id="project-1"),
            issue_key="GP-122",
            issue_summary="Need auth decision owner",
            issue_description="Security owner is Platform Security.",
            missing_slots=["decision_owner"],
            build_runtime_fn=lambda **_kwargs: object(),
            invoke_runtime_json_fn=_raise_runtime_error,
            project_repo_dir_fn=lambda **_kwargs: __import__("pathlib").Path(
                "/tmp/does-not-exist"
            ),
            runtime_error_type=_RuntimeErrorForTest,
        )


def test_resolve_slots_with_runtime_fails_on_invalid_answers_payload() -> None:
    with pytest.raises(
        RuntimeError, match="Runtime decision resolution returned invalid answers"
    ):
        resolve_slots_with_runtime(
            session=MagicMock(),
            settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/nope"),
            tenant=SimpleNamespace(tenant_id="tenant-1"),
            project=SimpleNamespace(project_id="project-1"),
            issue_key="GP-122",
            issue_summary="Need auth decision owner",
            issue_description="Security owner is Platform Security.",
            missing_slots=["decision_owner"],
            build_runtime_fn=lambda **_kwargs: object(),
            invoke_runtime_json_fn=MagicMock(return_value={"answers": []}),
            project_repo_dir_fn=lambda **_kwargs: __import__("pathlib").Path(
                "/tmp/does-not-exist"
            ),
            runtime_error_type=_RuntimeErrorForTest,
        )
