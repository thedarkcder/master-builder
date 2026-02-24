from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator.core.codex_invocation import CodexInvocationContext, invoke_codex_json
from orchestrator.core.codex_runtime import CodexRuntime


class CodexInvocationTests(unittest.TestCase):
    def test_turn_completed_log_line_is_persisted_even_when_sampled(self) -> None:
        def _request(  # noqa: ANN001
            _system_prompt,
            _user_prompt,
            _working_dir,
            on_log_line,
            _reasoning_effort,
            _resume_session_id,
            _on_session_id,
            _on_usage,
        ) -> str:
            if on_log_line is not None:
                for idx in range(1, 41):
                    on_log_line("stdout", f"noise-{idx}")
                on_log_line(
                    "stdout",
                    '{"type":"turn.completed","usage":{"input_tokens":77,"cached_input_tokens":55,"output_tokens":9}}',
                )
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = CodexInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="policy",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        class _Writer:
            def flush_invocation(self, *, invocation_id: str, timeout_seconds: float = 3.0) -> None:  # noqa: ARG002
                _ = invocation_id
                return None

        persisted_messages: list[str] = []

        def _capture_persist(*, context, stream: str, message: str) -> None:  # noqa: ANN001
            _ = context
            _ = stream
            persisted_messages.append(message)

        with (
            patch("orchestrator.core.codex_invocation._get_log_writer", return_value=_Writer()),
            patch("orchestrator.core.codex_invocation._enqueue_codex_log_line", side_effect=_capture_persist),
            patch("orchestrator.core.codex_invocation._append_raw_log_line"),
            patch("orchestrator.core.codex_invocation._emit_invocation_event"),
            patch("orchestrator.core.codex_invocation.get_settings") as settings_mock,
        ):
            settings_mock.return_value = type(
                "_Settings",
                (),
                {
                    "codex_db_log_sampling_interval": 1000,
                },
            )()
            payload = invoke_codex_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertTrue(
            any('"type":"turn.completed"' in message for message in persisted_messages),
            "turn.completed usage lines must bypass DB sampling",
        )

    def test_invoke_codex_json_captures_usage_from_turn_completed_log_line(self) -> None:
        def _request(  # noqa: ANN001
            _system_prompt,
            _user_prompt,
            _working_dir,
            on_log_line,
            _reasoning_effort,
            _resume_session_id,
            _on_session_id,
            _on_usage,
        ) -> str:
            if on_log_line is not None:
                on_log_line(
                    "stdout",
                    '{"type":"turn.completed","usage":{"input_tokens":51,"output_tokens":13,"total_tokens":64}}',
                )
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = CodexInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="test",
            working_dir=".",
            run_id="run-1",
        )

        class _Writer:
            def flush_invocation(self, *, invocation_id: str, timeout_seconds: float = 3.0) -> None:  # noqa: ARG002
                _ = invocation_id
                return None

        events: list[tuple[str, dict[str, object]]] = []

        def _capture_event(*, context, event_kind: str, payload: dict[str, object]) -> None:  # noqa: ANN001
            _ = context
            events.append((event_kind, payload))

        with (
            patch("orchestrator.core.codex_invocation._get_log_writer", return_value=_Writer()),
            patch("orchestrator.core.codex_invocation._emit_invocation_event", side_effect=_capture_event),
        ):
            payload = invoke_codex_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        finished_payload = next(
            payload
            for event_kind, payload in events
            if event_kind == "stage_invocation_finished"
        )
        self.assertEqual(finished_payload["actual_prompt_tokens"], 51)
        self.assertEqual(finished_payload["actual_completion_tokens"], 13)
        self.assertEqual(finished_payload["actual_total_tokens"], 64)
        self.assertEqual(finished_payload["actual_usage_observed"], True)

    def test_invoke_codex_json_flushes_invocation_logs(self) -> None:
        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=lambda _s, _u, _w, _l=None: '{"ok": true}',
        )
        context = CodexInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        class _Writer:
            def __init__(self) -> None:
                self.flushed_invocations: list[str] = []

            def flush_invocation(self, *, invocation_id: str, timeout_seconds: float = 3.0) -> None:
                _ = timeout_seconds
                self.flushed_invocations.append(invocation_id)

        writer = _Writer()
        with patch("orchestrator.core.codex_invocation._get_log_writer", return_value=writer):
            payload = invoke_codex_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(writer.flushed_invocations), 1)
        self.assertTrue(writer.flushed_invocations[0].strip())

    def test_invoke_codex_json_reuses_and_persists_codex_session(self) -> None:
        runtime_call: dict[str, str | None] = {}

        def _request(  # noqa: ANN001
            _system_prompt,
            _user_prompt,
            _working_dir,
            _on_log_line,
            _reasoning_effort,
            resume_session_id,
            on_session_id,
        ) -> str:
            runtime_call["resume_session_id"] = resume_session_id
            if on_session_id is not None:
                on_session_id("bbbbbbbb-cccc-dddd-eeee-ffffffffffff")
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = CodexInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        class _Writer:
            def flush_invocation(self, *, invocation_id: str, timeout_seconds: float = 3.0) -> None:  # noqa: ARG002
                _ = invocation_id
                return None

        with (
            patch("orchestrator.core.codex_invocation._get_log_writer", return_value=_Writer()),
            patch(
                "orchestrator.core.codex_invocation._load_run_codex_session_id",
                return_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ) as load_mock,
            patch("orchestrator.core.codex_invocation._persist_run_codex_session_id") as persist_mock,
        ):
            payload = invoke_codex_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(runtime_call["resume_session_id"], "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        load_mock.assert_called_with(run_id="run-1", session_column="pm_session_id")
        persist_mock.assert_called_with(
            run_id="run-1",
            session_id="bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
            session_column="pm_session_id",
        )

    def test_invoke_codex_json_uses_execution_session_bucket_for_dev(self) -> None:
        runtime_call: dict[str, str | None] = {}

        def _request(  # noqa: ANN001
            _system_prompt,
            _user_prompt,
            _working_dir,
            _on_log_line,
            _reasoning_effort,
            resume_session_id,
            on_session_id,
        ) -> str:
            runtime_call["resume_session_id"] = resume_session_id
            if on_session_id is not None:
                on_session_id("bbbbbbbb-cccc-dddd-eeee-ffffffffffff")
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = CodexInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="dev",
            working_dir=".",
            run_id="run-1",
        )

        class _Writer:
            def flush_invocation(self, *, invocation_id: str, timeout_seconds: float = 3.0) -> None:  # noqa: ARG002
                _ = invocation_id
                return None

        with (
            patch("orchestrator.core.codex_invocation._get_log_writer", return_value=_Writer()),
            patch("orchestrator.core.codex_invocation._load_run_codex_session_id", return_value=None) as load_mock,
            patch("orchestrator.core.codex_invocation._persist_run_codex_session_id") as persist_mock,
        ):
            payload = invoke_codex_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(runtime_call["resume_session_id"], None)
        load_mock.assert_called_with(run_id="run-1", session_column="dev_session_id")
        persist_mock.assert_called_with(
            run_id="run-1",
            session_id="bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
            session_column="dev_session_id",
        )

    def test_invoke_codex_json_emits_actual_usage_metrics(self) -> None:
        def _request(  # noqa: ANN001
            _system_prompt,
            _user_prompt,
            _working_dir,
            _on_log_line,
            _reasoning_effort,
            _resume_session_id,
            _on_session_id,
            on_usage,
        ) -> str:
            if on_usage is not None:
                on_usage(
                    {
                        "prompt_tokens": 41,
                        "completion_tokens": 9,
                        "total_tokens": 50,
                    }
                )
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = CodexInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        class _Writer:
            def flush_invocation(self, *, invocation_id: str, timeout_seconds: float = 3.0) -> None:  # noqa: ARG002
                _ = invocation_id
                return None

        events: list[tuple[str, dict[str, object]]] = []

        def _capture_event(*, context, event_kind: str, payload: dict[str, object]) -> None:  # noqa: ANN001
            _ = context
            events.append((event_kind, payload))

        with (
            patch("orchestrator.core.codex_invocation._get_log_writer", return_value=_Writer()),
            patch("orchestrator.core.codex_invocation._emit_invocation_event", side_effect=_capture_event),
        ):
            payload = invoke_codex_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        finished_payload = next(
            payload
            for event_kind, payload in events
            if event_kind == "stage_invocation_finished"
        )
        self.assertEqual(finished_payload["actual_prompt_tokens"], 41)
        self.assertEqual(finished_payload["actual_completion_tokens"], 9)
        self.assertEqual(finished_payload["actual_total_tokens"], 50)
        self.assertEqual(finished_payload["actual_usage_observed"], True)


if __name__ == "__main__":
    unittest.main()
