from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.knowledge.base import KnowledgeEmbeddingAccessMode
from orchestrator.core.runtime.invocation import (
    AgentInvocationContext,
    _AsyncRuntimeLogWriter,
    _emit_invocation_event,
    GovernedToolShellPolicyError,
    NativeToolPolicyError,
    RuntimeJsonContractError,
    ToolBridgeProtocolError,
    invoke_runtime_json,
    invoke_runtime_json_with_tools,
)
from orchestrator.core.runtime.runtime import CodexRuntime, CodexRuntimeError


class CodexInvocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._emit_invocation_event_patcher = patch("orchestrator.core.runtime.invocation._emit_invocation_event")
        self._emit_invocation_event_patcher.start()
        self.addCleanup(self._emit_invocation_event_patcher.stop)

    class _Writer:
        def enqueue(self, *, context, stream: str, message: str) -> bool:  # noqa: ANN001
            _ = context
            _ = stream
            _ = message
            return True

        def flush_invocation(self, *, invocation_id: str, timeout_seconds: float = 3.0) -> None:  # noqa: ARG002
            _ = invocation_id
            return None

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
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="policy",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        persisted_messages: list[str] = []

        def _capture_persist(*, context, stream: str, message: str) -> None:  # noqa: ANN001
            _ = context
            _ = stream
            persisted_messages.append(message)

        with (
            patch("orchestrator.core.runtime.invocation._enqueue_runtime_log_line", side_effect=_capture_persist),
            patch("orchestrator.core.runtime.invocation._append_raw_log_line"),
            patch("orchestrator.core.runtime.invocation._emit_invocation_event"),
            patch("orchestrator.core.runtime.invocation.get_settings") as settings_mock,
        ):
            settings_mock.return_value = type(
                "_Settings",
                (),
                {
                    "codex_db_log_sampling_interval": 1000,
                },
            )()
            payload = invoke_runtime_json(
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

    def test_async_runtime_log_writer_reports_persistence_failures_without_raising(self) -> None:
        writer = _AsyncRuntimeLogWriter()
        writer._batch_size = 100
        writer._batch_flush_ms = 50
        contexts = {
            invocation_id: AgentInvocationContext(
                channel="worker",
                tenant_id="tenant-1",
                project_id="proj-1",
                command="policy",
                stage="pm",
                working_dir=".",
                run_id=f"run-{invocation_id}",
                invocation_id=invocation_id,
            )
            for invocation_id in ("bad", "good")
        }
        persisted_groups: list[list[str]] = []

        def _persist_group(*, items):  # noqa: ANN001
            invocation_ids = [str(item.context.invocation_id) for item in items]
            persisted_groups.append(invocation_ids)
            if invocation_ids == ["bad"]:
                raise RuntimeError("bad invocation")

        with patch("orchestrator.core.runtime.invocation._persist_runtime_log_lines", side_effect=_persist_group):
            self.assertTrue(writer.enqueue(context=contexts["bad"], stream="stdout", message="bad line"))
            self.assertTrue(writer.enqueue(context=contexts["good"], stream="stdout", message="good line"))

            bad_flush = writer.flush_invocation(invocation_id="bad")
            good_flush = writer.flush_invocation(invocation_id="good")

        self.assertIn(["bad"], persisted_groups)
        self.assertIn(["good"], persisted_groups)
        self.assertEqual(bad_flush.failure_message, "Runtime log persistence failed for invocation_id=bad: bad invocation")
        self.assertTrue(good_flush.completed)
        self.assertIsNone(good_flush.failure_message)

    def test_invoke_runtime_json_captures_usage_from_turn_completed_log_line(self) -> None:
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
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="test",
            working_dir=".",
            run_id="run-1",
        )

        events: list[tuple[str, dict[str, object]]] = []

        def _capture_event(*, context, event_kind: str, payload: dict[str, object]) -> None:  # noqa: ANN001
            _ = context
            events.append((event_kind, payload))

        with (
            patch("orchestrator.core.runtime.invocation._emit_invocation_event", side_effect=_capture_event),
        ):
            payload = invoke_runtime_json(
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

    def test_runtime_log_sink_redacts_sensitive_content_before_persistence(self) -> None:
        captured_db: list[str] = []

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
                    "stderr",
                    (
                        "run_id=123e4567-e89b-12d3-a456-426614174000 "
                        "APP_STORE_CONNECT_API_KEY_BASE64=super-secret-value "
                        "email=user@example.com"
                    ),
                )
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="test",
            working_dir=".",
            run_id="run-1",
        )

        def _capture_db(*, context, stream: str, message: str) -> None:  # noqa: ANN001
            _ = context
            _ = stream
            captured_db.append(message)

        with (
            patch("orchestrator.core.runtime.invocation._append_raw_log_line"),
            patch("orchestrator.core.runtime.invocation._enqueue_runtime_log_line", side_effect=_capture_db),
            patch("orchestrator.core.runtime.invocation._emit_invocation_event"),
        ):
            payload = invoke_runtime_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(captured_db), 1)
        for message in captured_db:
            self.assertNotIn("super-secret-value", message)
            self.assertNotIn("user@example.com", message)
            self.assertIn("123e4567-e89b-12d3-a456-426614174000", message)
            self.assertIn("[REDACTED]", message)

    def test_invoke_runtime_json_does_not_flush_async_runtime_logs(self) -> None:
        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=lambda _s, _u, _w, _l=None: '{"ok": true}',
        )
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        class _CaptureWriter:
            def __init__(self) -> None:
                self.enqueued_messages: list[str] = []
                self.flushed_invocations: list[str] = []

            def enqueue(self, *, context, stream: str, message: str) -> bool:  # noqa: ANN001
                _ = context
                _ = stream
                self.enqueued_messages.append(message)
                return True

            def flush_invocation(self, *, invocation_id: str, timeout_seconds: float = 3.0) -> None:
                _ = timeout_seconds
                self.flushed_invocations.append(invocation_id)

        writer = _CaptureWriter()
        with patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=writer):
            payload = invoke_runtime_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(writer.flushed_invocations, [])

    def test_invoke_runtime_json_does_not_fail_when_async_runtime_log_enqueue_fails(self) -> None:
        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=lambda _s, _u, _w, on_log_line=None: (
                on_log_line("stdout", "line") if callable(on_log_line) else None
            )
            or '{"ok": true}',
        )
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="test",
            working_dir=".",
            run_id="run-1",
        )
        events: list[tuple[str, dict[str, object]]] = []

        class _FailingEnqueueWriter:
            def enqueue(self, *, context, stream: str, message: str) -> bool:  # noqa: ANN001
                _ = context
                _ = stream
                _ = message
                return False

            def flush_invocation(self, *, invocation_id: str, timeout_seconds: float = 3.0) -> None:
                raise AssertionError("runtime execution must not flush async log writes")

        def _capture_event(*, context, event_kind: str, payload: dict[str, object]) -> None:  # noqa: ANN001
            _ = context
            events.append((event_kind, payload))

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=_FailingEnqueueWriter()),
            patch("orchestrator.core.runtime.invocation._emit_invocation_event", side_effect=_capture_event),
        ):
            payload = invoke_runtime_json(
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
        self.assertEqual(finished_payload["status"], "succeeded")
        self.assertEqual(finished_payload["runtime_log_persistence_failed"], True)
        self.assertIn("Runtime log write failed", str(finished_payload["runtime_log_persistence_error"]))

    def test_runtime_invocation_event_persistence_failure_is_non_terminal(self) -> None:
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="test",
            working_dir=".",
            run_id="run-1",
            invocation_id="invocation-1",
            db_session=object(),  # type: ignore[arg-type]
        )

        with (
            patch(
                "orchestrator.core.runtime.invocation.record_observability_stream_event",
                side_effect=RuntimeError("ClickHouse query failed: 500 NOT_ENOUGH_SPACE"),
            ),
            patch(
                "orchestrator.core.runtime.invocation.get_settings",
                return_value=SimpleNamespace(agent_id="agent-1"),
            ),
            self.assertLogs("orchestrator.core.runtime.invocation", level="ERROR") as logs,
        ):
            _emit_invocation_event(
                context=context,
                event_kind="stage_invocation_finished",
                payload={"status": "succeeded"},
            )

        self.assertTrue(
            any("runtime_invocation_event_persist_failed" in message for message in logs.output),
            logs.output,
        )

    def test_invoke_runtime_json_uses_explicit_codex_session_and_persists_it(self) -> None:
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
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            workflow_id="workflow-1",
            run_id="run-1",
            codex_session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )

        with (
            patch("orchestrator.core.runtime.invocation._persist_checkpoint_session_id") as persist_mock,
        ):
            payload = invoke_runtime_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(runtime_call["resume_session_id"], "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        persist_mock.assert_called_with(
            workflow_id="workflow-1",
            run_id="run-1",
            session_id="bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
            checkpoint_kind="pm",
            stage="pm",
        )

    def test_invoke_runtime_json_uses_execution_session_bucket_for_dev(self) -> None:
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
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="dev",
            working_dir=".",
            workflow_id="workflow-1",
            run_id="run-1",
        )

        with (
            patch("orchestrator.core.runtime.invocation._persist_checkpoint_session_id") as persist_mock,
        ):
            payload = invoke_runtime_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(runtime_call["resume_session_id"], None)
        persist_mock.assert_called_with(
            workflow_id="workflow-1",
            run_id="run-1",
            session_id="bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
            checkpoint_kind="execution",
            stage="dev",
        )

    def test_invoke_runtime_json_uses_orchestrated_session_bucket(self) -> None:
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
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="orchestrated_run",
            working_dir=".",
            workflow_id="workflow-1",
            run_id="run-1",
        )

        with (
            patch("orchestrator.core.runtime.invocation._persist_checkpoint_session_id") as persist_mock,
        ):
            payload = invoke_runtime_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(runtime_call["resume_session_id"], None)
        persist_mock.assert_called_with(
            workflow_id="workflow-1",
            run_id="run-1",
            session_id="bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
            checkpoint_kind="orchestrated",
            stage="orchestrated_run",
        )

    def test_invoke_runtime_json_does_not_resume_fresh_attempt_from_checkpoint_storage(self) -> None:
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
                on_session_id("fresh-session-id")
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            workflow_id="workflow-1",
            run_id="run-1",
        )

        with (
            patch("orchestrator.core.runtime.invocation._persist_checkpoint_session_id"),
        ):
            payload = invoke_runtime_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertIsNone(runtime_call["resume_session_id"])

    def test_invoke_runtime_json_emits_actual_usage_metrics(self) -> None:
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
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        events: list[tuple[str, dict[str, object]]] = []

        def _capture_event(*, context, event_kind: str, payload: dict[str, object]) -> None:  # noqa: ANN001
            _ = context
            events.append((event_kind, payload))

        with (
            patch("orchestrator.core.runtime.invocation._emit_invocation_event", side_effect=_capture_event),
        ):
            payload = invoke_runtime_json(
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

    def test_invoke_runtime_json_normalizes_provider_usage_metrics(self) -> None:
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
                        "input_tokens": 41,
                        "cached_input_tokens": 17,
                        "output_tokens": 9,
                    }
                )
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=_request,
        )
        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        events: list[tuple[str, dict[str, object]]] = []

        def _capture_event(*, context, event_kind: str, payload: dict[str, object]) -> None:  # noqa: ANN001
            _ = context
            events.append((event_kind, payload))

        with (
            patch("orchestrator.core.runtime.invocation._emit_invocation_event", side_effect=_capture_event),
        ):
            payload = invoke_runtime_json(
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
        self.assertEqual(finished_payload["actual_cached_input_tokens"], 17)
        self.assertEqual(finished_payload["actual_usage_observed"], True)

    def test_invoke_runtime_json_passes_effective_policy_model_to_runtime(self) -> None:
        captured: dict[str, object] = {}

        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                captured.update(kwargs)
                return {"ok": True}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        with (
            patch(
                "orchestrator.core.runtime.invocation._resolve_knowledge_policy_for_context",
                return_value=("proj-1", True, "aggressive", "gpt-5.4-mini", "high", True),
            ),
            patch("orchestrator.core.runtime.invocation.create_session_factory"),
            patch("orchestrator.core.runtime.invocation.build_knowledge_prompt_context", return_value=SimpleNamespace(text="", citations=[])),
        ):
            payload = invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
        )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(captured["model_override"], "gpt-5.4-mini")
        self.assertEqual(captured["reasoning_effort"], "high")

    def test_invoke_runtime_json_uses_best_effort_knowledge_lookup_for_worker_execution(self) -> None:
        class _Runtime:
            def run_json(self, **_kwargs):  # noqa: ANN003
                return {"ok": True}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="dev",
            working_dir=".",
            run_id="run-1",
        )

        with (
            patch(
                "orchestrator.core.runtime.invocation._resolve_knowledge_policy_for_context",
                return_value=("proj-1", True, "aggressive", "gpt-5.4", "medium", False),
            ),
            patch("orchestrator.core.runtime.invocation.create_session_factory"),
            patch(
                "orchestrator.core.runtime.invocation.build_knowledge_prompt_context",
                return_value=SimpleNamespace(text="", citations=[]),
            ) as knowledge_mock,
        ):
            payload = invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(
            knowledge_mock.call_args.kwargs["embedding_access_mode"],
            KnowledgeEmbeddingAccessMode.BEST_EFFORT,
        )

    def test_invoke_runtime_json_prefers_scoped_reasoning_override_over_context_default(self) -> None:
        captured: dict[str, object] = {}

        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                captured.update(kwargs)
                return {"ok": True}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            run_id="run-1",
            reasoning_effort="medium",
        )

        with (
            patch(
                "orchestrator.core.runtime.invocation._resolve_knowledge_policy_for_context",
                return_value=("proj-1", True, "aggressive", "gpt-5.4-mini", "high", True),
            ),
            patch("orchestrator.core.runtime.invocation.create_session_factory"),
            patch(
                "orchestrator.core.runtime.invocation.build_knowledge_prompt_context",
                return_value=SimpleNamespace(text="", citations=[]),
            ),
        ):
            payload = invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(captured["reasoning_effort"], "high")

    def test_invoke_runtime_json_uses_scoped_model_when_kb_injection_disabled(self) -> None:
        captured: dict[str, object] = {}

        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                captured.update(kwargs)
                return {"ok": True}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        with (
            patch(
                "orchestrator.core.runtime.invocation._resolve_knowledge_policy_for_context",
                return_value=("proj-1", False, "aggressive", "gpt-5.4-mini", "high", True),
            ) as resolve_policy_mock,
            patch("orchestrator.core.runtime.invocation.get_settings") as settings_mock,
        ):
            settings_mock.return_value = type(
                "_Settings",
                (),
                {
                    "knowledge_injection_enabled": False,
                    "database_url": "sqlite:///ignored.db",
                    "codex_model": "gpt-5.4",
                    "codex_reasoning_effort": "medium",
                    "codex_db_log_sampling_interval": 1,
                },
            )()
            payload = invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        resolve_policy_mock.assert_called_once_with(context=context)
        self.assertEqual(captured["model_override"], "gpt-5.4-mini")
        self.assertEqual(captured["reasoning_effort"], "high")

    def test_invoke_runtime_json_prefers_http_runtime_profile_model_over_policy_model(self) -> None:
        captured: dict[str, object] = {}

        class _Runtime:
            command = "http:lm_studio"
            model = "openai/gpt-oss-20b"

            def run_json(self, **kwargs):  # noqa: ANN003
                captured.update(kwargs)
                return {"ok": True}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="project_automation",
            stage="standup_voice_brief",
            working_dir=".",
            run_id="run-1",
        )

        with (
            patch(
                "orchestrator.core.runtime.invocation._resolve_knowledge_policy_for_context",
                return_value=("proj-1", True, "aggressive", "gpt-5.4", "high", True),
            ),
            patch("orchestrator.core.runtime.invocation.create_session_factory"),
            patch("orchestrator.core.runtime.invocation.build_knowledge_prompt_context", return_value=SimpleNamespace(text="", citations=[])),
        ):
            payload = invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(captured["model_override"], "openai/gpt-oss-20b")
        self.assertEqual(captured["reasoning_effort"], "high")

    def test_invoke_runtime_json_fails_on_non_json_payload(self) -> None:
        class _Runtime:
            def run_json(self, **_kwargs):  # noqa: ANN003
                raise CodexRuntimeError(
                    "Invalid JSON payload from Codex runtime: Expecting value",
                    payload_preview="not-json",
                )

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="dev",
            working_dir=".",
            run_id="run-1",
        )

        with (
            self.assertRaises(RuntimeJsonContractError) as raised,
        ):
            invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertIn("invalid json payload", str(raised.exception).lower())

    def test_invoke_runtime_json_does_not_reclassify_runtime_failures_as_json_contract_errors(self) -> None:
        class _Runtime:
            def run_json(self, **_kwargs):  # noqa: ANN003
                raise CodexRuntimeError("Codex CLI command failed with exit code 2: unknown option --json")

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="dev",
            working_dir=".",
            run_id="run-1",
        )

        with (
            self.assertRaises(CodexRuntimeError),
        ):
            invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

    def test_invoke_runtime_json_with_tools_executes_request_and_resumes_same_session(self) -> None:
        runtime_calls: list[dict[str, object]] = []
        tool_calls: list[tuple[str, dict[str, object]]] = []

        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                runtime_calls.append(kwargs)
                if len(runtime_calls) == 1:
                    on_session_id = kwargs.get("on_session_id")
                    if callable(on_session_id):
                        on_session_id("tool-session-1")
                    return {
                        "type": "tool_request",
                        "tool_name": "decision.read_state",
                        "tool_args": {"issue_key": "GP-124"},
                    }
                return {
                    "type": "final_response",
                    "result": {"gate_status": "clear", "reason": "", "questions": []},
                }

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
        )

        with patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()):
            payload = invoke_runtime_json_with_tools(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools={"decision.read_state"},
                execute_tool=lambda tool_name, tool_args: tool_calls.append((tool_name, tool_args)) or {"case": "ok"},
            )

        self.assertEqual(payload, {"gate_status": "clear", "reason": "", "questions": []})
        self.assertEqual(tool_calls, [("decision.read_state", {"issue_key": "GP-124"})])
        self.assertIsNone(runtime_calls[0]["resume_session_id"])
        self.assertEqual(runtime_calls[1]["resume_session_id"], "tool-session-1")
        self.assertIn('"tool_name": "decision.read_state"', str(runtime_calls[1]["user_prompt"]))
        self.assertIn('"ok": true', str(runtime_calls[1]["user_prompt"]).lower())

    def test_invoke_runtime_json_retries_after_disallowed_native_tool_observation(self) -> None:
        class _Runtime:
            command = "codex"

            def __init__(self) -> None:
                self.user_prompts: list[str] = []

            def run_json(self, **kwargs):  # noqa: ANN003
                self.user_prompts.append(str(kwargs.get("user_prompt") or ""))
                on_log_line = kwargs.get("on_log_line")
                if len(self.user_prompts) == 1 and callable(on_log_line):
                    on_log_line("stdout", '{"type":"web_search_call","query":"jira api"}')
                return {"ok": True, "attempt": len(self.user_prompts)}

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
        )
        runtime = _Runtime()

        with patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()):
            payload = invoke_runtime_json(
                runtime=runtime,  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True, "attempt": 2})
        self.assertEqual(len(runtime.user_prompts), 2)
        self.assertIn("web.search", runtime.user_prompts[1])
        self.assertIn("Do not rely on evidence gathered from the disallowed tool use", runtime.user_prompts[1])

    def test_invoke_runtime_json_retries_after_telemetry_wrapped_disallowed_native_tool_observation(self) -> None:
        class _Runtime:
            command = "codex"

            def __init__(self) -> None:
                self.user_prompts: list[str] = []

            def run_json(self, **kwargs):  # noqa: ANN003
                self.user_prompts.append(str(kwargs.get("user_prompt") or ""))
                on_log_line = kwargs.get("on_log_line")
                if len(self.user_prompts) == 1 and callable(on_log_line):
                    import json as _json

                    on_log_line(
                        "stdout",
                        _json.dumps(
                            {
                                "event_type": "runtime_log",
                                "message": _json.dumps(
                                    {
                                        "type": "item.started",
                                        "item": {
                                            "id": "item_33",
                                            "type": "web_search",
                                            "query": "",
                                        },
                                    }
                                ),
                            }
                        ),
                    )
                return {"ok": True, "attempt": len(self.user_prompts)}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="qa",
            working_dir=".",
            run_id="run-1",
        )
        runtime = _Runtime()

        with patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()):
            payload = invoke_runtime_json(
                runtime=runtime,  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_native_tools=set(),
            )

        self.assertEqual(payload, {"ok": True, "attempt": 2})
        self.assertEqual(len(runtime.user_prompts), 2)
        self.assertIn("web.search", runtime.user_prompts[1])

    def test_invoke_runtime_json_fails_when_native_tool_policy_repair_also_violates_policy(self) -> None:
        class _Runtime:
            command = "codex"

            def run_json(self, **kwargs):  # noqa: ANN003
                on_log_line = kwargs.get("on_log_line")
                if callable(on_log_line):
                    on_log_line("stdout", '{"type":"web_search_call","query":"jira api"}')
                return {"ok": True}

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
        )

        with (
            self.assertRaises(NativeToolPolicyError) as raised,
        ):
            invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertIn("web.search", str(raised.exception))

    def test_invoke_runtime_json_allows_stage_allowed_native_tool_observation(self) -> None:
        class _Runtime:
            command = "codex"

            def run_json(self, **kwargs):  # noqa: ANN003
                on_log_line = kwargs.get("on_log_line")
                if callable(on_log_line):
                    on_log_line("stdout", '{"type":"web_search_call","query":"jira api"}')
                return {"ok": True}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="dev",
            working_dir=".",
        )

        with patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()):
            payload = invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})

    def test_invoke_runtime_json_preserves_operation_id_in_runtime_log_sink(self) -> None:
        captured: dict[str, object] = {}
        live_lines: list[tuple[str | None, str | None, str]] = []

        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                on_log_line = kwargs.get("on_log_line")
                if callable(on_log_line):
                    on_log_line("stdout", "planning line")
                return {"ok": True}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="engineering_planning",
            working_dir=".",
            workflow_id="parent_planning:MAB-215",
            operation_id="operation-jira-child-fanout",
            attempt_id="attempt-1",
            run_id="run-1",
        )

        def _fake_sink(**kwargs):  # noqa: ANN003
            captured.update(kwargs)
            return lambda _stream, _message: None

        def _capture_live_line(*, context, stream: str, message: str) -> None:  # noqa: ANN001
            _ = stream
            live_lines.append((context.operation_id, context.attempt_id, message))

        with (
            patch("orchestrator.core.runtime.invocation.build_runtime_log_sink", side_effect=_fake_sink),
            patch("orchestrator.core.runtime.invocation._emit_invocation_event"),
            patch("orchestrator.core.runtime.invocation._append_raw_log_line"),
            patch("orchestrator.core.runtime.invocation._enqueue_runtime_log_line", side_effect=_capture_live_line),
        ):
            payload = invoke_runtime_json(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(captured["workflow_id"], "parent_planning:MAB-215")
        self.assertEqual(captured["operation_id"], "operation-jira-child-fanout")
        self.assertEqual(captured["attempt_id"], "attempt-1")
        self.assertEqual(live_lines, [("operation-jira-child-fanout", "attempt-1", "planning line")])

    def test_invoke_runtime_json_with_tools_preserves_operation_attempt_context_and_emits_tool_events(self) -> None:
        sink_contexts: list[tuple[str | None, str | None]] = []
        invocation_events: list[tuple[str, str | None, str | None, dict[str, object]]] = []

        class _Runtime:
            def __init__(self) -> None:
                self._calls = 0

            def run_json(self, **kwargs):  # noqa: ANN003
                self._calls += 1
                if self._calls == 1:
                    on_session_id = kwargs.get("on_session_id")
                    if callable(on_session_id):
                        on_session_id("tool-session-1")
                    return {
                        "type": "tool_request",
                        "tool_name": "decision.read_state",
                        "tool_args": {"issue_key": "GP-124"},
                    }
                return {
                    "type": "final_response",
                    "result": {"gate_status": "clear"},
                }

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
            workflow_id="parent_planning:MAB-215",
            operation_id="operation-jira-child-fanout",
            attempt_id="attempt-7",
        )

        def _fake_sink(**kwargs):  # noqa: ANN003
            sink_contexts.append((kwargs.get("operation_id"), kwargs.get("attempt_id")))
            return lambda _stream, _message: None

        def _capture_event(*, context, event_kind: str, payload: dict[str, object]) -> None:  # noqa: ANN001
            invocation_events.append((event_kind, context.operation_id, context.attempt_id, payload))

        with (
            patch("orchestrator.core.runtime.invocation.build_runtime_log_sink", side_effect=_fake_sink),
            patch("orchestrator.core.runtime.invocation._emit_invocation_event", side_effect=_capture_event),
            patch("orchestrator.core.runtime.invocation._append_raw_log_line"),
            patch("orchestrator.core.runtime.invocation._enqueue_runtime_log_line"),
        ):
            payload = invoke_runtime_json_with_tools(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools={"decision.read_state"},
                execute_tool=lambda _tool_name, _tool_args: {"case": "ok"},
            )

        self.assertEqual(payload, {"gate_status": "clear"})
        self.assertEqual(
            sink_contexts,
            [
                ("operation-jira-child-fanout", "attempt-7"),
                ("operation-jira-child-fanout", "attempt-7"),
            ],
        )
        self.assertIn(
            (
                "tool_request",
                "operation-jira-child-fanout",
                "attempt-7",
                {"message": "Requested tool decision.read_state.", "tool_name": "decision.read_state", "tool_hop": 1, "tool_args": {"issue_key": "GP-124"}},
            ),
            invocation_events,
        )
        self.assertIn(
            (
                "tool_result",
                "operation-jira-child-fanout",
                "attempt-7",
                {"message": "Completed tool decision.read_state.", "tool_name": "decision.read_state", "tool_hop": 1, "ok": True, "tool_result": {"case": "ok"}},
            ),
            invocation_events,
        )

    def test_invoke_runtime_json_with_tools_rejects_raw_git_push_shell_bypass(self) -> None:
        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                on_log_line = kwargs.get("on_log_line")
                if callable(on_log_line):
                    on_log_line(
                        "stdout",
                        (
                            '{"type":"response_item","payload":{"type":"function_call",'
                            '"name":"exec_command","arguments":"{\\"cmd\\":'
                            '\\"git push -u origin feature/CAP-7\\",'
                            '\\"workdir\\":\\"/tmp/repo\\"}"}}'
                        ),
                    )
                return {"type": "final_response", "result": {"message": "published"}}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="dev",
            working_dir=".",
            run_id="run-1",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            self.assertRaises(GovernedToolShellPolicyError) as raised,
        ):
            invoke_runtime_json_with_tools(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools={"github.push_branch", "github.open_pr"},
                execute_tool=lambda _tool_name, _tool_args: {"ok": True},
            )

        self.assertIn("raw git push", str(raised.exception))
        self.assertIn("github.push_branch", str(raised.exception))

    def test_invoke_runtime_json_with_tools_rejects_raw_gh_pr_shell_bypass(self) -> None:
        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                on_log_line = kwargs.get("on_log_line")
                if callable(on_log_line):
                    on_log_line(
                        "stdout",
                        (
                            '{"type":"response_item","payload":{"type":"function_call",'
                            '"name":"exec_command","arguments":"{\\"cmd\\":'
                            '\\"gh pr create --base master --head feature/CAP-7\\",'
                            '\\"workdir\\":\\"/tmp/repo\\"}"}}'
                        ),
                    )
                return {"type": "final_response", "result": {"message": "published"}}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="dev",
            working_dir=".",
            run_id="run-1",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            self.assertRaises(GovernedToolShellPolicyError) as raised,
        ):
            invoke_runtime_json_with_tools(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools={"github.push_branch", "github.open_pr"},
                execute_tool=lambda _tool_name, _tool_args: {"ok": True},
            )

        self.assertIn("raw gh pr command", str(raised.exception))
        self.assertIn("github.open_pr", str(raised.exception))

    def test_invoke_runtime_json_with_tools_rejects_raw_github_api_shell_bypass(self) -> None:
        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                on_log_line = kwargs.get("on_log_line")
                if callable(on_log_line):
                    on_log_line(
                        "stdout",
                        (
                            '{"type":"response_item","payload":{"type":"function_call",'
                            '"name":"exec_command","arguments":"{\\"cmd\\":'
                            '\\"curl -s \\\\\\"https://api.github.com/repos/thedarkcder/consumer-app/pulls?'
                            'head=thedarkcder:feature/CAP-8&state=open\\\\\\"\\",'
                            '\\"workdir\\":\\"/tmp/repo\\"}"}}'
                        ),
                    )
                return {"type": "final_response", "result": {"message": "published"}}

        context = AgentInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="dev",
            working_dir=".",
            run_id="run-1",
        )

        with (
            patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()),
            self.assertRaises(GovernedToolShellPolicyError) as raised,
        ):
            invoke_runtime_json_with_tools(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools={"github.push_branch", "github.open_pr", "github.get_pr_details"},
                execute_tool=lambda _tool_name, _tool_args: {"ok": True},
            )

        self.assertIn("raw GitHub API command", str(raised.exception))
        self.assertIn("github.get_pr_details", str(raised.exception))

    def test_invoke_runtime_json_with_tools_fails_on_disallowed_tool(self) -> None:
        runtime_calls: list[dict[str, object]] = []

        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                runtime_calls.append(kwargs)
                if len(runtime_calls) == 1:
                    return {
                        "type": "tool_request",
                        "tool_name": "decision.read_state",
                        "tool_args": {},
                    }
                return {
                    "type": "final_response",
                    "result": {"message": "continued without disallowed tool"},
                }

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
        )

        with (
            self.assertRaises(ToolBridgeProtocolError) as raised,
        ):
            invoke_runtime_json_with_tools(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools=set(),
                execute_tool=lambda _tool_name, _tool_args: {"ok": True},
            )

        self.assertIn("disallowed tool", str(raised.exception).lower())
        self.assertEqual(len(runtime_calls), 1)

    def test_invoke_runtime_json_with_tools_requests_final_response_after_tool_hop_limit(self) -> None:
        runtime_calls: list[dict[str, object]] = []
        test_case = self

        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                runtime_calls.append(kwargs)
                if len(runtime_calls) <= 2:
                    return {
                        "type": "tool_request",
                        "tool_name": "decision.read_state",
                        "tool_args": {},
                    }
                test_case.assertIn("Runtime tool hop limit 1 reached", kwargs["user_prompt"])
                test_case.assertIn("Return a final_response now", kwargs["user_prompt"])
                return {
                    "type": "final_response",
                    "result": {"message": "continued after hop limit"},
                }

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
        )

        with patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()):
            result = invoke_runtime_json_with_tools(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools={"decision.read_state"},
                execute_tool=lambda _tool_name, _tool_args: {"ok": True},
                max_tool_hops=1,
            )

        self.assertEqual(result, {"message": "continued after hop limit"})
        self.assertEqual(len(runtime_calls), 3)

    def test_invoke_runtime_json_with_tools_fails_when_bridge_never_finalizes(self) -> None:
        class _Runtime:
            def run_json(self, **_kwargs):  # noqa: ANN003
                return {
                    "type": "tool_request",
                    "tool_name": "decision.read_state",
                    "tool_args": {},
                }

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
        )

        with (
            self.assertRaises(ToolBridgeProtocolError) as raised,
        ):
            invoke_runtime_json_with_tools(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools=set(),
                execute_tool=lambda _tool_name, _tool_args: {"ok": True},
            )

        self.assertIn("disallowed tool", str(raised.exception).lower())

    def test_invoke_runtime_json_with_tools_returns_correctable_tool_errors_to_runtime(self) -> None:
        runtime_calls: list[dict[str, object]] = []
        test_case = self

        class _Runtime:
            def run_json(self, **kwargs):  # noqa: ANN003
                runtime_calls.append(kwargs)
                if len(runtime_calls) == 1:
                    return {
                        "type": "tool_request",
                        "tool_name": "repo.read",
                        "tool_args": {"command": "rg foo . | head"},
                    }
                test_case.assertIn("\"ok\": false", kwargs["user_prompt"])
                test_case.assertIn("no shell operators", kwargs["user_prompt"])
                test_case.assertIn("corrected allowed tool_request", kwargs["user_prompt"])
                test_case.assertNotIn("Tool failures are advisory", kwargs["user_prompt"])
                return {
                    "type": "final_response",
                    "result": {"message": "corrected"},
                }

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
        )

        def _reject_tool_request(_tool_name: str, _tool_args: dict[str, object]) -> dict[str, object]:
            raise PermissionError("repo.read only allows a single command (no shell operators)")

        with patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()):
            result = invoke_runtime_json_with_tools(
                runtime=_Runtime(),  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools={"repo.read"},
                execute_tool=_reject_tool_request,
            )

        self.assertEqual(result, {"message": "corrected"})
        self.assertEqual(len(runtime_calls), 2)

    def test_invoke_runtime_json_with_tools_returns_tool_executor_failure_to_model(self) -> None:
        class _Runtime:
            def __init__(self) -> None:
                self.user_prompts: list[str] = []

            def run_json(self, **kwargs):  # noqa: ANN003
                self.user_prompts.append(str(kwargs.get("user_prompt") or ""))
                if len(self.user_prompts) == 1:
                    return {
                        "type": "tool_request",
                        "tool_name": "decision.read_state",
                        "tool_args": {},
                    }
                return {
                    "type": "final_response",
                    "result": {"state": "blocked", "reason": "tool database unavailable"},
                }

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
        )

        def _raise_tool_failure(_tool_name: str, _tool_args: dict[str, object]) -> dict[str, object]:
            raise RuntimeError("tool database unavailable")

        runtime = _Runtime()
        with patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()):
            result = invoke_runtime_json_with_tools(
                runtime=runtime,  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools={"decision.read_state"},
                execute_tool=_raise_tool_failure,
            )

        self.assertEqual(result, {"state": "blocked", "reason": "tool database unavailable"})
        self.assertEqual(len(runtime.user_prompts), 2)
        self.assertIn('"failure_policy": "recoverable_tool_failure"', runtime.user_prompts[1])
        self.assertIn("tool database unavailable", runtime.user_prompts[1])

    def test_invoke_runtime_json_with_tools_repairs_false_governed_tool_unavailable_block(self) -> None:
        test_case = self

        class _Runtime:
            def __init__(self) -> None:
                self.user_prompts: list[str] = []

            def run_json(self, **kwargs):  # noqa: ANN003
                self.user_prompts.append(str(kwargs.get("user_prompt") or ""))
                if len(self.user_prompts) == 1:
                    return {
                        "type": "final_response",
                        "result": {
                            "outcome": "blocked",
                            "blocker_message": (
                                "Local implementation and validation are complete, but this runtime does not "
                                "expose a callable governed GitHub/Jira publication bridge for the mandatory PR handoff. "
                                "`tool_search` returned 0 matching tools for `github.push_branch` and `github.open_pr`."
                            ),
                        },
                    }
                if len(self.user_prompts) == 2:
                    test_case.assertIn(
                        "incorrectly treated allowed governed tools as unavailable",
                        kwargs["user_prompt"],
                    )
                    test_case.assertIn("github.push_branch", kwargs["user_prompt"])
                    test_case.assertIn("Original prompt:\noriginal task context", kwargs["user_prompt"])
                    return {
                        "type": "tool_request",
                        "tool_name": "github.push_branch",
                        "tool_args": {"branch_name": "feature/GP-113"},
                    }
                test_case.assertIn('"tool_name": "github.push_branch"', kwargs["user_prompt"])
                return {
                    "type": "final_response",
                    "result": {"outcome": "continue", "change_summary": ["published branch"]},
                }

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="workflow",
            stage="dev",
            working_dir=".",
        )
        runtime = _Runtime()

        with patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()):
            result = invoke_runtime_json_with_tools(
                runtime=runtime,  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="original task context",
                allowed_tools={"github.push_branch", "github.open_pr"},
                execute_tool=lambda _name, _args: {"ok": True},
            )

        self.assertEqual(result, {"outcome": "continue", "change_summary": ["published branch"]})
        self.assertEqual(len(runtime.user_prompts), 3)

    def test_invoke_runtime_json_with_tools_repairs_native_codex_publication_false_block(self) -> None:
        test_case = self

        class _Runtime:
            def __init__(self) -> None:
                self.user_prompts: list[str] = []

            def run_json(self, **kwargs):  # noqa: ANN003
                self.user_prompts.append(str(kwargs.get("user_prompt") or ""))
                if len(self.user_prompts) == 1:
                    return {
                        "type": "final_response",
                        "result": {
                            "outcome": "blocked",
                            "blocker_message": (
                                "Validated code is green locally, but the required governed PR publication step "
                                "could not be executed from this native Codex tool path, so `pr_url` is still missing."
                            ),
                        },
                    }
                if len(self.user_prompts) == 2:
                    test_case.assertIn(
                        "incorrectly treated allowed governed tools as unavailable",
                        kwargs["user_prompt"],
                    )
                    return {
                        "type": "tool_request",
                        "tool_name": "github.push_branch",
                        "tool_args": {"branch_name": "feature/GP-113"},
                    }
                return {
                    "type": "final_response",
                    "result": {"outcome": "continue", "change_summary": ["published branch"]},
                }

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="workflow",
            stage="dev",
            working_dir=".",
        )
        runtime = _Runtime()

        with patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()):
            result = invoke_runtime_json_with_tools(
                runtime=runtime,  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="original task context",
                allowed_tools={"github.push_branch", "github.open_pr"},
                execute_tool=lambda _name, _args: {"ok": True},
            )

        self.assertEqual(result, {"outcome": "continue", "change_summary": ["published branch"]})
        self.assertEqual(len(runtime.user_prompts), 3)

    def test_invoke_runtime_json_with_tools_native_policy_repair_keeps_original_prompt(self) -> None:
        class _Runtime:
            command = "codex"

            def __init__(self) -> None:
                self.user_prompts: list[str] = []

            def run_json(self, **kwargs):  # noqa: ANN003
                self.user_prompts.append(str(kwargs.get("user_prompt") or ""))
                if len(self.user_prompts) == 1:
                    return {
                        "type": "tool_request",
                        "tool_name": "decision.read_state",
                        "tool_args": {},
                    }
                on_log_line = kwargs.get("on_log_line")
                if len(self.user_prompts) == 2 and callable(on_log_line):
                    on_log_line("stdout", '{"type":"web_search_call","query":"jira api"}')
                return {"type": "final_response", "result": {"state": "ok"}}

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
        )
        runtime = _Runtime()

        with patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()):
            result = invoke_runtime_json_with_tools(
                runtime=runtime,  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="original task context",
                allowed_tools={"decision.read_state"},
                execute_tool=lambda _name, _args: {"answered": True},
            )

        self.assertEqual(result, {"state": "ok"})
        self.assertEqual(len(runtime.user_prompts), 3)
        self.assertIn("original task context", runtime.user_prompts[2])
        self.assertNotIn("Tool result:", runtime.user_prompts[2])

    def test_required_correctable_tool_failure_remains_required_evidence(self) -> None:
        class _Runtime:
            def __init__(self) -> None:
                self.user_prompts: list[str] = []

            def run_json(self, **kwargs):  # noqa: ANN003
                self.user_prompts.append(str(kwargs.get("user_prompt") or ""))
                if len(self.user_prompts) == 1:
                    return {
                        "type": "tool_request",
                        "tool_name": "decision.read_state",
                        "tool_args": {"bad": True},
                    }
                return {
                    "type": "final_response",
                    "result": {"state": "blocked"},
                }

        context = AgentInvocationContext(
            channel="system",
            tenant_id="tenant-1",
            project_id=None,
            command="policy",
            stage="decision_planner",
            working_dir=".",
        )
        runtime = _Runtime()

        with patch("orchestrator.core.runtime.invocation._get_log_writer", return_value=self._Writer()):
            invoke_runtime_json_with_tools(
                runtime=runtime,  # type: ignore[arg-type]
                context=context,
                system_prompt="system",
                user_prompt="user",
                allowed_tools={"decision.read_state"},
                required_tools={"decision.read_state"},
                execute_tool=lambda _name, _args: (_ for _ in ()).throw(ValueError("bad args")),
            )

        self.assertIn("This failed tool is required evidence", runtime.user_prompts[1])


if __name__ == "__main__":
    unittest.main()
