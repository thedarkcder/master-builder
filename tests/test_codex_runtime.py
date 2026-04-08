from __future__ import annotations

import subprocess
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.codex_runtime import (
    CodexRuntime,
    CodexRuntimeError,
    _extract_json_payload,
    _extract_session_id_from_json_line,
    _extract_usage_from_json_stdout,
    _openai_compatible_base_url_for_local_server,
    build_codex_runtime,
    build_http_runtime,
    build_runtime_with_fallback,
)


class CodexRuntimeErrorTests(unittest.TestCase):
    def test_str_includes_payload_preview(self) -> None:
        err = CodexRuntimeError("Runtime HTTP request failed with status 400", payload_preview='{"error":"Unknown model"}')
        self.assertIn("400", str(err))
        self.assertIn("Unknown model", str(err))


class OpenAiCompatibleBaseUrlTests(unittest.TestCase):
    def test_lm_studio_appends_v1_when_missing(self) -> None:
        self.assertEqual(
            _openai_compatible_base_url_for_local_server(base_url="http://127.0.0.1:1234", runtime_kind="lm_studio"),
            "http://127.0.0.1:1234/v1",
        )

    def test_lm_studio_preserves_existing_v1(self) -> None:
        self.assertEqual(
            _openai_compatible_base_url_for_local_server(base_url="http://host.docker.internal:1234/v1", runtime_kind="lm_studio"),
            "http://host.docker.internal:1234/v1",
        )


class ExtractJsonPayloadTests(unittest.TestCase):
    def test_extract_json_payload_variants(self) -> None:
        self.assertEqual(_extract_json_payload('{"ok": true}'), {"ok": True})
        self.assertEqual(_extract_json_payload('```json\n{"x":1}\n```'), {"x": 1})
        self.assertEqual(_extract_json_payload('prefix {"k":"v"} suffix'), {"k": "v"})

    def test_extract_json_payload_errors(self) -> None:
        with self.assertRaises(CodexRuntimeError):
            _extract_json_payload("```json\\n{bad}\\n```")
        with self.assertRaises(CodexRuntimeError):
            _extract_json_payload("no json here")

    def test_extract_json_payload_parses_ndjson_candidate_lines(self) -> None:
        payload = _extract_json_payload(
            '{"type":"turn.started"}\n{"decision_gate_required":false,"reason":"ok"}'
        )
        self.assertEqual(payload, {"decision_gate_required": False, "reason": "ok"})

    def test_extract_json_payload_prefers_terminal_json_object(self) -> None:
        payload = _extract_json_payload(
            '{"type":"turn.started"}{"decision_gate_required":false,"reason":"ok"}'
        )
        self.assertEqual(payload, {"decision_gate_required": False, "reason": "ok"})

    def test_extract_session_id_from_json_line_accepts_thread_and_session_meta(self) -> None:
        self.assertEqual(
            _extract_session_id_from_json_line(
                '{"type":"thread.started","thread_id":"11111111-2222-3333-4444-555555555555"}'
            ),
            "11111111-2222-3333-4444-555555555555",
        )
        self.assertEqual(
            _extract_session_id_from_json_line(
                '{"type":"session_meta","payload":{"id":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}}'
            ),
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )

    def test_extract_usage_from_json_stdout_detects_prompt_and_completion_tokens(self) -> None:
        usage = _extract_usage_from_json_stdout(
            [
                '{"type":"response.started"}\n',
                '{"type":"response.completed","payload":{"usage":{"input_tokens":101,"output_tokens":33,"total_tokens":134}}}\n',
            ]
        )
        self.assertEqual(
            usage,
            {"prompt_tokens": 101, "completion_tokens": 33, "total_tokens": 134},
        )


class CodexRuntimeTests(unittest.TestCase):
    def test_run_text_and_json(self) -> None:
        runtime = CodexRuntime(
            model="m",
            max_output_tokens=1000,
            command="override",
            _request=lambda _s, _u, _w, _l=None: " {\"ok\": true} ",
        )
        text = runtime.run_text(system_prompt="sys", user_prompt="usr")
        self.assertEqual(text, '{"ok": true}')
        payload = runtime.run_json(system_prompt="sys", user_prompt="usr")
        self.assertEqual(payload, {"ok": True})
        logged: list[tuple[str, str]] = []
        runtime.run_text(
            system_prompt="sys",
            user_prompt="usr",
            on_log_line=lambda stream, message: logged.append((stream, message)),
        )
        self.assertEqual(logged, [])

        empty_runtime = CodexRuntime(
            model="m",
            max_output_tokens=1000,
            command="override",
            _request=lambda _s, _u, _w, _l=None: "   ",
        )
        with self.assertRaises(CodexRuntimeError):
            empty_runtime.run_text(system_prompt="s", user_prompt="u")

        non_dict_runtime = CodexRuntime(
            model="m",
            max_output_tokens=1000,
            command="override",
            _request=lambda _s, _u, _w, _l=None: "[1,2]",
        )
        with self.assertRaises(CodexRuntimeError):
            non_dict_runtime.run_json(system_prompt="s", user_prompt="u")

    def test_run_json_forwards_usage_callback(self) -> None:
        captured_usage: dict[str, int] = {}

        def _request(
            _system_prompt: str,
            _user_prompt: str,
            _working_dir: str | None,
            _on_log_line: object,
            _reasoning_effort: str | None,
            _resume_session_id: str | None,
            _on_session_id: object,
            on_usage: object,
        ) -> str:
            if on_usage is not None:
                assert callable(on_usage)
                on_usage(
                    {
                        "prompt_tokens": 12,
                        "completion_tokens": 5,
                        "total_tokens": 17,
                    }
                )
            return '{"ok": true}'

        runtime = CodexRuntime(
            model="m",
            max_output_tokens=1000,
            command="override",
            _request=_request,
        )
        payload = runtime.run_json(
            system_prompt="sys",
            user_prompt="usr",
            on_usage=lambda usage: captured_usage.update(usage),
        )
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(captured_usage, {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17})

    def test_runtime_with_fallback_uses_secondary_runtime_after_primary_failure(self) -> None:
        primary_runtime = CodexRuntime(
            model="primary",
            max_output_tokens=1000,
            command="primary",
            _request=lambda *_args, **_kwargs: (_ for _ in ()).throw(CodexRuntimeError("primary failed")),
        )
        fallback_runtime = CodexRuntime(
            model="fallback",
            max_output_tokens=1000,
            command="fallback",
            _request=lambda *_args, **_kwargs: '{"ok": true, "source": "fallback"}',
        )
        runtime = build_runtime_with_fallback(
            primary_runtime=primary_runtime,
            fallback_runtime=fallback_runtime,
        )
        self.assertEqual(
            runtime.run_json(system_prompt="sys", user_prompt="usr"),
            {"ok": True, "source": "fallback"},
        )


class BuildHttpRuntimeTests(unittest.TestCase):
    def _settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            database_url="postgresql+psycopg://orchestrator:orchestrator@postgres:5432/orchestrator",
            codex_model="gpt-5.4",
            codex_max_output_tokens=4096,
            codex_cli_command="codex",
            codex_sandbox_mode="workspace-write",
            codex_tool_database_url="",
            codex_reasoning_effort="medium",
            codex_stderr_log_mode="all",
            codex_hang_detection_quiet_seconds=300,
            codex_hang_detection_report_interval_seconds=120,
        )

    def test_openai_runtime_preserves_message_history_across_resume_calls(self) -> None:
        settings = self._settings()
        request_payloads: list[dict[str, object]] = []

        class _FakeHttpResponse:
            def __init__(self, payload: dict[str, object]) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
                return None

        responses = iter(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "type": "tool_request",
                                        "tool_name": "decision.read_state",
                                        "tool_args": {"issue_key": "GP-124"},
                                    }
                                )
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "type": "final_response",
                                        "result": {"status": "ok"},
                                    }
                                )
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26},
                },
            ]
        )

        def fake_urlopen(request, timeout=120):  # noqa: ANN001, ARG001
            request_payloads.append(json.loads(request.data.decode("utf-8")))
            return _FakeHttpResponse(next(responses))

        with patch("orchestrator.core.codex_runtime.urllib_request.urlopen", side_effect=fake_urlopen):
            runtime = build_http_runtime(
                settings=settings,
                runtime_kind="openai",
                base_url="https://example-openai.test/v1",
                api_key="secret",
            )
            captured_session_ids: list[str] = []
            first = runtime.run_json(
                system_prompt="system",
                user_prompt="initial user request",
                on_session_id=lambda session_id: captured_session_ids.append(session_id),
            )
            second = runtime.run_json(
                system_prompt="system",
                user_prompt='Tool result:\n{"tool_name":"decision.read_state","ok":true,"result":{"case":"ok"}}',
                resume_session_id=captured_session_ids[0],
                on_session_id=lambda session_id: captured_session_ids.append(session_id),
            )

        self.assertEqual(first["type"], "tool_request")
        self.assertEqual(second["type"], "final_response")
        self.assertEqual(len(captured_session_ids), 2)
        self.assertEqual(captured_session_ids[0], captured_session_ids[1])

        first_messages = request_payloads[0]["messages"]
        second_messages = request_payloads[1]["messages"]
        self.assertEqual(
            first_messages,
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "initial user request"},
            ],
        )
        self.assertEqual(
            second_messages,
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "initial user request"},
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "type": "tool_request",
                            "tool_name": "decision.read_state",
                            "tool_args": {"issue_key": "GP-124"},
                        }
                    ),
                },
                {
                    "role": "user",
                    "content": 'Tool result:\n{"tool_name":"decision.read_state","ok":true,"result":{"case":"ok"}}',
                },
            ],
        )

    def test_claude_runtime_preserves_message_history_across_resume_calls(self) -> None:
        settings = self._settings()
        request_payloads: list[dict[str, object]] = []

        class _FakeHttpResponse:
            def __init__(self, payload: dict[str, object]) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
                return None

        responses = iter(
            [
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "type": "tool_request",
                                    "tool_name": "decision.read_state",
                                    "tool_args": {"issue_key": "GP-124"},
                                }
                            ),
                        }
                    ]
                },
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "type": "final_response",
                                    "result": {"status": "ok"},
                                }
                            ),
                        }
                    ]
                },
            ]
        )

        def fake_urlopen(request, timeout=120):  # noqa: ANN001, ARG001
            request_payloads.append(json.loads(request.data.decode("utf-8")))
            return _FakeHttpResponse(next(responses))

        with patch("orchestrator.core.codex_runtime.urllib_request.urlopen", side_effect=fake_urlopen):
            runtime = build_http_runtime(
                settings=settings,
                runtime_kind="claude",
                base_url="https://example-claude.test",
                api_key="secret",
            )
            captured_session_ids: list[str] = []
            first = runtime.run_json(
                system_prompt="system",
                user_prompt="initial user request",
                on_session_id=lambda session_id: captured_session_ids.append(session_id),
            )
            second = runtime.run_json(
                system_prompt="system",
                user_prompt='Tool result:\n{"tool_name":"decision.read_state","ok":true,"result":{"case":"ok"}}',
                resume_session_id=captured_session_ids[0],
                on_session_id=lambda session_id: captured_session_ids.append(session_id),
            )

        self.assertEqual(first["type"], "tool_request")
        self.assertEqual(second["type"], "final_response")
        self.assertEqual(captured_session_ids[0], captured_session_ids[1])
        self.assertEqual(request_payloads[0]["system"], "system")
        self.assertEqual(
            request_payloads[0]["messages"],
            [{"role": "user", "content": "initial user request"}],
        )
        self.assertEqual(
            request_payloads[1]["messages"],
            [
                {"role": "user", "content": "initial user request"},
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "type": "tool_request",
                            "tool_name": "decision.read_state",
                            "tool_args": {"issue_key": "GP-124"},
                        }
                    ),
                },
                {
                    "role": "user",
                    "content": 'Tool result:\n{"tool_name":"decision.read_state","ok":true,"result":{"case":"ok"}}',
                },
            ],
        )


class BuildCodexRuntimeTests(unittest.TestCase):
    def _settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            database_url="postgresql+psycopg://orchestrator:orchestrator@postgres:5432/orchestrator",
            codex_model="gpt-5-codex",
            codex_max_output_tokens=4096,
            codex_cli_command="codex",
            codex_sandbox_mode="workspace-write",
            codex_tool_database_url="",
            codex_reasoning_effort="medium",
            codex_stderr_log_mode="all",
            codex_hang_detection_quiet_seconds=300,
            codex_hang_detection_report_interval_seconds=120,
        )

    def test_build_with_request_override(self) -> None:
        settings = self._settings()
        runtime = build_codex_runtime(
            settings=settings,
            request_override=lambda _s, _u, _w=None: "ok",
        )
        self.assertEqual(runtime.command, "override")

    def test_build_cli_command_validation(self) -> None:
        settings = self._settings()
        settings.codex_cli_command = ""
        with self.assertRaises(CodexRuntimeError):
            build_codex_runtime(settings=settings)

        settings = self._settings()
        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value=None),
        ):
            with self.assertRaises(CodexRuntimeError):
                build_codex_runtime(settings=settings)

    def test_cli_request_success_and_fallbacks(self) -> None:
        settings = self._settings()

        class _FakePipe:
            def __init__(self, lines: list[str] | None = None) -> None:
                self._lines = list(lines or [])
                self._index = 0

            def readline(self) -> str:
                if self._index >= len(self._lines):
                    return ""
                line = self._lines[self._index]
                self._index += 1
                return line

            def close(self) -> None:
                return None

        class _FakeStdin:
            def write(self, _content: str) -> None:
                return None

            def close(self) -> None:
                return None

        class _FakePopen:
            def __init__(
                self,
                output_path: str,
                *,
                returncode: int = 0,
                stdout_lines: list[str] | None = None,
                stderr_lines: list[str] | None = None,
                output_text: str = "",
            ) -> None:
                self.stdin = _FakeStdin()
                self.stdout = _FakePipe(stdout_lines)
                self.stderr = _FakePipe(stderr_lines)
                self._returncode = returncode
                Path(output_path).write_text(output_text, encoding="utf-8")

            def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
                return self._returncode

            def kill(self) -> None:
                return None

        def fake_popen_write_output(args: list[str], **_kwargs: object):
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(args[output_idx], output_text="json-output")

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen_write_output) as popen_mock,
        ):
            runtime = build_codex_runtime(settings=settings)
            self.assertEqual(
                runtime.run_text(system_prompt="s", user_prompt="u", working_dir="/tmp/repo"),
                "json-output",
            )
            self.assertEqual(popen_mock.call_args.kwargs["cwd"], "/tmp/repo")
            self.assertEqual(
                popen_mock.call_args.kwargs["env"]["ORCHESTRATOR_DATABASE_URL"],
                settings.database_url,
            )

    def test_cli_request_uses_explicit_tool_database_url_override_when_configured(self) -> None:
        settings = self._settings()
        settings.codex_tool_database_url = "postgresql+psycopg://orchestrator:orchestrator@127.0.0.1:5500/orchestrator"

        class _FakePipe:
            def readline(self) -> str:
                return ""

            def close(self) -> None:
                return None

        class _FakeStdin:
            def write(self, _content: str) -> None:
                return None

            def close(self) -> None:
                return None

        class _FakePopen:
            def __init__(self, output_path: str) -> None:
                self.stdin = _FakeStdin()
                self.stdout = _FakePipe()
                self.stderr = _FakePipe()
                Path(output_path).write_text("json-output", encoding="utf-8")

            def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
                return 0

            def kill(self) -> None:
                return None

        def fake_popen(args: list[str], **_kwargs: object):
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(args[output_idx])

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen) as popen_mock,
        ):
            runtime = build_codex_runtime(settings=settings)
            self.assertEqual(runtime.run_text(system_prompt="s", user_prompt="u"), "json-output")

        self.assertEqual(
            popen_mock.call_args.kwargs["env"]["ORCHESTRATOR_DATABASE_URL"],
            settings.codex_tool_database_url,
        )

    def test_cli_request_preserves_non_postgres_tool_database_url(self) -> None:
        settings = self._settings()
        settings.codex_tool_database_url = "sqlite:////tmp/orchestrator-test.db"

        class _FakePipe:
            def readline(self) -> str:
                return ""

            def close(self) -> None:
                return None

        class _FakeStdin:
            def write(self, _content: str) -> None:
                return None

            def close(self) -> None:
                return None

        class _FakePopen:
            def __init__(self, output_path: str) -> None:
                self.stdin = _FakeStdin()
                self.stdout = _FakePipe()
                self.stderr = _FakePipe()
                Path(output_path).write_text("json-output", encoding="utf-8")

            def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
                return 0

            def kill(self) -> None:
                return None

        def fake_popen(args: list[str], **_kwargs: object):
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(args[output_idx])

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen) as popen_mock,
        ):
            runtime = build_codex_runtime(settings=settings)
            self.assertEqual(runtime.run_text(system_prompt="s", user_prompt="u"), "json-output")

        self.assertEqual(
            popen_mock.call_args.kwargs["env"]["ORCHESTRATOR_DATABASE_URL"],
            settings.codex_tool_database_url,
        )

    def test_cli_request_uses_model_override_when_provided(self) -> None:
        settings = self._settings()

        class _FakePipe:
            def __init__(self, lines: list[str] | None = None) -> None:
                self._lines = list(lines or [])
                self._index = 0

            def readline(self) -> str:
                if self._index >= len(self._lines):
                    return ""
                line = self._lines[self._index]
                self._index += 1
                return line

            def close(self) -> None:
                return None

        class _FakeStdin:
            def write(self, _content: str) -> None:
                return None

            def close(self) -> None:
                return None

        class _FakePopen:
            def __init__(
                self,
                output_path: str,
                *,
                stdout_lines: list[str] | None = None,
                output_text: str = "json-output",
            ) -> None:
                self.stdin = _FakeStdin()
                self.stdout = _FakePipe(stdout_lines)
                self.stderr = _FakePipe()
                Path(output_path).write_text(output_text, encoding="utf-8")

            def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
                return 0

            def kill(self) -> None:
                return None

        def fake_popen(args: list[str], **_kwargs: object):
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(args[output_idx])

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen) as popen_mock,
        ):
            runtime = build_codex_runtime(settings=settings)
            runtime.run_text(system_prompt="s", user_prompt="u", model_override="gpt-5.3-codex-spark")

        args = popen_mock.call_args.args[0]
        self.assertEqual(args[args.index("--model") + 1], "gpt-5.3-codex-spark")
        self.assertEqual(args[2:6], ["--disable", "apps", "--disable", "plugins"])
        call_args = list(popen_mock.call_args.args[0])
        sandbox_idx = call_args.index("--sandbox") + 1
        self.assertEqual(call_args[sandbox_idx], "workspace-write")
        config_idx = call_args.index("-c") + 1
        self.assertEqual(call_args[config_idx], 'reasoning.effort="medium"')

        def fake_popen_stdout(args: list[str], **_kwargs: object):
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(args[output_idx], stdout_lines=["stdout-output\n"], output_text="")

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen_stdout),
        ):
            runtime = build_codex_runtime(settings=settings)
            self.assertEqual(runtime.run_text(system_prompt="s", user_prompt="u"), "stdout-output")

    def test_cli_request_waits_with_polling_timeout(self) -> None:
        settings = self._settings()

        class _FakePipe:
            def readline(self) -> str:
                return ""

            def close(self) -> None:
                return None

        class _FakeStdin:
            def write(self, _content: str) -> None:
                return None

            def close(self) -> None:
                return None

        class _FakePopen:
            def __init__(self, output_path: str) -> None:
                self.stdin = _FakeStdin()
                self.stdout = _FakePipe()
                self.stderr = _FakePipe()
                self.last_wait_timeout: float | None = 123.0
                Path(output_path).write_text("json-output", encoding="utf-8")

            def wait(self, timeout: float | None = None) -> int:
                self.last_wait_timeout = timeout
                return 0

            def kill(self) -> None:
                return None

        holder: dict[str, _FakePopen] = {}

        def fake_popen(args: list[str], **_kwargs: object):
            output_idx = args.index("--output-last-message") + 1
            proc = _FakePopen(args[output_idx])
            holder["proc"] = proc
            return proc

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen),
        ):
            runtime = build_codex_runtime(settings=settings)
            self.assertEqual(runtime.run_text(system_prompt="s", user_prompt="u"), "json-output")
            self.assertIn("proc", holder)
            self.assertEqual(holder["proc"].last_wait_timeout, 1.0)

    def test_cli_request_reports_suspected_hang_without_failing(self) -> None:
        settings = self._settings()
        settings.codex_hang_detection_quiet_seconds = 30
        settings.codex_hang_detection_report_interval_seconds = 15

        class _FakePipe:
            def readline(self) -> str:
                return ""

            def close(self) -> None:
                return None

        class _FakeStdin:
            def write(self, _content: str) -> None:
                return None

            def close(self) -> None:
                return None

        class _FakePopen:
            def __init__(self, output_path: str) -> None:
                self.stdin = _FakeStdin()
                self.stdout = _FakePipe()
                self.stderr = _FakePipe()
                self.pid = 4321
                self._wait_calls = 0
                Path(output_path).write_text('{"ok": true}', encoding="utf-8")

            def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
                self._wait_calls += 1
                if self._wait_calls <= 3:
                    raise subprocess.TimeoutExpired(cmd="codex", timeout=1.0)
                return 0

            def kill(self) -> None:
                return None

        def fake_popen(args: list[str], **_kwargs: object):
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(args[output_idx])

        # Monotonic timeline:
        # initial=0, mark_activity=0, then three polling loops at 40/60/80 seconds idle.
        monotonic_values = iter([0.0, 0.0, 40.0, 60.0, 80.0, 95.0, 110.0, 125.0, 140.0])

        def _fake_monotonic() -> float:
            try:
                return next(monotonic_values)
            except StopIteration:
                return 140.0

        with (
            patch("orchestrator.core.codex_runtime.time.monotonic", side_effect=_fake_monotonic),
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen),
        ):
            runtime = build_codex_runtime(settings=settings)
            logged: list[tuple[str, str]] = []
            payload = runtime.run_json(
                system_prompt="s",
                user_prompt="u",
                on_log_line=lambda stream, message: logged.append((stream, message)),
            )
            self.assertEqual(payload, {"ok": True})
            self.assertTrue(
                any(
                    stream == "system" and "codex_process_suspected_hung" in message
                    for stream, message in logged
                )
            )

    def test_cli_request_streams_log_lines(self) -> None:
        settings = self._settings()

        class _FakePipe:
            def __init__(self, lines: list[str]) -> None:
                self._lines = list(lines)
                self._index = 0

            def readline(self) -> str:
                if self._index >= len(self._lines):
                    return ""
                line = self._lines[self._index]
                self._index += 1
                return line

            def close(self) -> None:
                return None

        class _FakeStdin:
            def write(self, _content: str) -> None:
                return None

            def close(self) -> None:
                return None

        class _FakePopen:
            def __init__(self, output_path: str) -> None:
                self.stdin = _FakeStdin()
                self.stdout = _FakePipe(["out-1\n", "out-2\n"])
                self.stderr = _FakePipe(["err-1\n"])
                Path(output_path).write_text('{"ok": true}', encoding="utf-8")

            def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
                return 0

            def kill(self) -> None:
                return None

        def fake_popen(args: list[str], **_kwargs: object):
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(args[output_idx])

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen),
        ):
            runtime = build_codex_runtime(settings=settings)
            logged: list[tuple[str, str]] = []
            payload = runtime.run_json(
                system_prompt="s",
                user_prompt="u",
                on_log_line=lambda stream, message: logged.append((stream, message)),
            )
            self.assertEqual(payload, {"ok": True})
            self.assertEqual(logged, [("stdout", "out-1"), ("stdout", "out-2"), ("stderr", "err-1")])

    def test_cli_request_suppresses_stderr_log_lines_when_disabled(self) -> None:
        settings = self._settings()
        settings.codex_stderr_log_mode = "off"

        class _FakePipe:
            def __init__(self, lines: list[str]) -> None:
                self._lines = list(lines)
                self._index = 0

            def readline(self) -> str:
                if self._index >= len(self._lines):
                    return ""
                line = self._lines[self._index]
                self._index += 1
                return line

            def close(self) -> None:
                return None

        class _FakeStdin:
            def write(self, _content: str) -> None:
                return None

            def close(self) -> None:
                return None

        class _FakePopen:
            def __init__(self, output_path: str) -> None:
                self.stdin = _FakeStdin()
                self.stdout = _FakePipe(["out-1\n"])
                self.stderr = _FakePipe(["thinking\n", "error: broken\n"])
                Path(output_path).write_text('{"ok": true}', encoding="utf-8")

            def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
                return 0

            def kill(self) -> None:
                return None

        def fake_popen(args: list[str], **_kwargs: object):
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(args[output_idx])

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen),
        ):
            runtime = build_codex_runtime(settings=settings)
            logged: list[tuple[str, str]] = []
            payload = runtime.run_json(
                system_prompt="s",
                user_prompt="u",
                on_log_line=lambda stream, message: logged.append((stream, message)),
            )
            self.assertEqual(payload, {"ok": True})
            self.assertEqual(logged, [("stdout", "out-1")])

    def test_cli_request_emits_only_error_stderr_lines_when_configured(self) -> None:
        settings = self._settings()
        settings.codex_stderr_log_mode = "errors_only"

        class _FakePipe:
            def __init__(self, lines: list[str]) -> None:
                self._lines = list(lines)
                self._index = 0

            def readline(self) -> str:
                if self._index >= len(self._lines):
                    return ""
                line = self._lines[self._index]
                self._index += 1
                return line

            def close(self) -> None:
                return None

        class _FakeStdin:
            def write(self, _content: str) -> None:
                return None

            def close(self) -> None:
                return None

        class _FakePopen:
            def __init__(self, output_path: str) -> None:
                self.stdin = _FakeStdin()
                self.stdout = _FakePipe(["out-1\n"])
                self.stderr = _FakePipe(["thinking\n", "fatal: boom\n", "done\n"])
                Path(output_path).write_text('{"ok": true}', encoding="utf-8")

            def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
                return 0

            def kill(self) -> None:
                return None

        def fake_popen(args: list[str], **_kwargs: object):
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(args[output_idx])

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen),
        ):
            runtime = build_codex_runtime(settings=settings)
            logged: list[tuple[str, str]] = []
            payload = runtime.run_json(
                system_prompt="s",
                user_prompt="u",
                on_log_line=lambda stream, message: logged.append((stream, message)),
            )
            self.assertEqual(payload, {"ok": True})
            self.assertEqual(logged, [("stdout", "out-1"), ("stderr", "fatal: boom")])

    def test_cli_request_failures(self) -> None:
        settings = self._settings()

        class _FakePipe:
            def __init__(self, lines: list[str] | None = None) -> None:
                self._lines = list(lines or [])
                self._index = 0

            def readline(self) -> str:
                if self._index >= len(self._lines):
                    return ""
                line = self._lines[self._index]
                self._index += 1
                return line

            def close(self) -> None:
                return None

        class _FakeStdin:
            def write(self, _content: str) -> None:
                return None

            def close(self) -> None:
                return None

        class _FakePopen:
            def __init__(
                self,
                output_path: str,
                *,
                returncode: int = 0,
                stdout_lines: list[str] | None = None,
                stderr_lines: list[str] | None = None,
                output_text: str = "",
            ) -> None:
                self.stdin = _FakeStdin()
                self.stdout = _FakePipe(stdout_lines)
                self.stderr = _FakePipe(stderr_lines)
                self._returncode = returncode
                Path(output_path).write_text(output_text, encoding="utf-8")

            def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
                return self._returncode

            def kill(self) -> None:
                return None

        def fake_popen_auth(args: list[str], **_kwargs: object):
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(args[output_idx], returncode=1, stderr_lines=["auth required\n"])

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen_auth),
        ):
            runtime = build_codex_runtime(settings=settings)
            with self.assertRaises(CodexRuntimeError) as exc_info:
                runtime.run_text(system_prompt="s", user_prompt="u")
            self.assertIn("docker compose run --rm run-worker codex login --device-auth", str(exc_info.exception))

        def fake_popen_auth_with_link(args: list[str], **_kwargs: object):
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(
                args[output_idx],
                returncode=1,
                stderr_lines=["auth required https://auth.openai.com/device/abc123\n"],
            )

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen_auth_with_link),
        ):
            runtime = build_codex_runtime(settings=settings)
            with self.assertRaises(CodexRuntimeError) as exc_info:
                runtime.run_text(system_prompt="s", user_prompt="u")
            self.assertIn("https://auth.openai.com/device/abc123", str(exc_info.exception))

        def fake_popen_boom(args: list[str], **_kwargs: object):
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(args[output_idx], returncode=2, stderr_lines=["boom\n"])

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen_boom),
        ):
            runtime = build_codex_runtime(settings=settings)
            with self.assertRaises(CodexRuntimeError):
                runtime.run_text(system_prompt="s", user_prompt="u")

        def fake_popen_structured_limit(args: list[str], **_kwargs: object):
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(
                args[output_idx],
                returncode=1,
                stdout_lines=[
                    '{"type":"error","message":"You\'ve hit your usage limit for GPT-5.3-Codex-Spark. Switch to another model now, or try again later."}\n',
                    '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit for GPT-5.3-Codex-Spark. Switch to another model now, or try again later."}}\n',
                ],
                stderr_lines=["Warning: no last agent message; wrote empty content to /tmp/tmp.txt\n"],
            )

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen_structured_limit),
        ):
            runtime = build_codex_runtime(settings=settings)
            with self.assertRaises(CodexRuntimeError) as exc_info:
                runtime.run_text(system_prompt="s", user_prompt="u")
            self.assertIn("usage limit", str(exc_info.exception).lower())
            self.assertNotIn("no last agent message", str(exc_info.exception).lower())

        def fake_popen_empty(args: list[str], **_kwargs: object):
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(args[output_idx], output_text="")

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen_empty),
        ):
            runtime = build_codex_runtime(settings=settings)
            with self.assertRaises(CodexRuntimeError):
                runtime.run_text(system_prompt="s", user_prompt="u")

    def test_cli_resume_uses_session_id_and_parses_json_response(self) -> None:
        settings = self._settings()

        class _FakePipe:
            def __init__(self, lines: list[str]) -> None:
                self._lines = list(lines)
                self._index = 0

            def readline(self) -> str:
                if self._index >= len(self._lines):
                    return ""
                line = self._lines[self._index]
                self._index += 1
                return line

            def close(self) -> None:
                return None

        class _FakeStdin:
            def write(self, _content: str) -> None:
                return None

            def close(self) -> None:
                return None

        class _FakePopen:
            def __init__(self) -> None:
                self.stdin = _FakeStdin()
                self.stdout = _FakePipe(
                    [
                        '{"type":"thread.started","thread_id":"11111111-2222-3333-4444-555555555555"}\n',
                        '{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"resume-output"}]}}\n',
                    ]
                )
                self.stderr = _FakePipe([])

            def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
                return 0

            def kill(self) -> None:
                return None

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", return_value=_FakePopen()) as popen_mock,
        ):
            runtime = build_codex_runtime(settings=settings)
            captured_session_ids: list[str] = []
            output = runtime.run_text(
                system_prompt="s",
                user_prompt="u",
                resume_session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                on_session_id=lambda session_id: captured_session_ids.append(session_id),
            )
            self.assertEqual(output, "resume-output")
            call_args = list(popen_mock.call_args.args[0])
            self.assertEqual(call_args[:4], ["codex", "exec", "resume", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"])
            self.assertEqual(call_args[4:8], ["--disable", "apps", "--disable", "plugins"])
            self.assertNotIn("--sandbox", call_args)
            self.assertIn("--full-auto", call_args)
            self.assertIn("--json", call_args)
            self.assertNotIn("--output-last-message", call_args)
            self.assertEqual(captured_session_ids, ["11111111-2222-3333-4444-555555555555"])

    def test_cli_resume_extracts_agent_message_from_item_completed_events(self) -> None:
        settings = self._settings()

        class _FakePipe:
            def __init__(self, lines: list[str]) -> None:
                self._lines = list(lines)
                self._index = 0

            def readline(self) -> str:
                if self._index >= len(self._lines):
                    return ""
                line = self._lines[self._index]
                self._index += 1
                return line

            def close(self) -> None:
                return None

        class _FakeStdin:
            def write(self, _content: str) -> None:
                return None

            def close(self) -> None:
                return None

        class _FakePopen:
            def __init__(self) -> None:
                self.stdin = _FakeStdin()
                self.stdout = _FakePipe(
                    [
                        '{"type":"turn.started"}\n',
                        '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"decision_gate_required\\": false, \\"reason\\": \\"ok\\"}"}}\n',
                    ]
                )
                self.stderr = _FakePipe([])

            def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
                return 0

            def kill(self) -> None:
                return None

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", return_value=_FakePopen()),
        ):
            runtime = build_codex_runtime(settings=settings)
            payload = runtime.run_json(
                system_prompt="s",
                user_prompt="u",
                resume_session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            )
            self.assertEqual(payload, {"decision_gate_required": False, "reason": "ok"})

    def test_cli_resume_uses_danger_flag_for_danger_full_access(self) -> None:
        settings = self._settings()
        settings.codex_sandbox_mode = "danger-full-access"

        class _FakePipe:
            def __init__(self, lines: list[str]) -> None:
                self._lines = list(lines)
                self._index = 0

            def readline(self) -> str:
                if self._index >= len(self._lines):
                    return ""
                line = self._lines[self._index]
                self._index += 1
                return line

            def close(self) -> None:
                return None

        class _FakeStdin:
            def write(self, _content: str) -> None:
                return None

            def close(self) -> None:
                return None

        class _FakePopen:
            def __init__(self) -> None:
                self.stdin = _FakeStdin()
                self.stdout = _FakePipe(
                    [
                        '{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}}\n',
                    ]
                )
                self.stderr = _FakePipe([])

            def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
                return 0

            def kill(self) -> None:
                return None

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", return_value=_FakePopen()) as popen_mock,
        ):
            runtime = build_codex_runtime(settings=settings)
            output = runtime.run_text(
                system_prompt="s",
                user_prompt="u",
                resume_session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            )
            self.assertEqual(output, "ok")
            call_args = list(popen_mock.call_args.args[0])
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", call_args)


if __name__ == "__main__":
    unittest.main()
