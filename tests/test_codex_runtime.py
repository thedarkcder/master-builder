from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.codex_runtime import (
    CodexRuntime,
    CodexRuntimeError,
    _extract_json_payload,
    build_codex_runtime,
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


class BuildCodexRuntimeTests(unittest.TestCase):
    def _settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            codex_model="gpt-5-codex",
            codex_max_output_tokens=4096,
            codex_cli_command="codex",
            codex_sandbox_mode="workspace-write",
            codex_reasoning_effort="medium",
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

        def fake_popen_write_output(args, **kwargs):  # noqa: ANN001
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
            call_args = list(popen_mock.call_args.args[0])
            sandbox_idx = call_args.index("--sandbox") + 1
            self.assertEqual(call_args[sandbox_idx], "workspace-write")
            config_idx = call_args.index("-c") + 1
            self.assertEqual(call_args[config_idx], 'reasoning.effort="medium"')

        def fake_popen_stdout(args, **kwargs):  # noqa: ANN001
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(args[output_idx], stdout_lines=["stdout-output\n"], output_text="")

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen_stdout),
        ):
            runtime = build_codex_runtime(settings=settings)
            self.assertEqual(runtime.run_text(system_prompt="s", user_prompt="u"), "stdout-output")

    def test_cli_request_waits_without_timeout(self) -> None:
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

        def fake_popen(args, **kwargs):  # noqa: ANN001
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
            self.assertIsNone(holder["proc"].last_wait_timeout)

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

        def fake_popen(args, **kwargs):  # noqa: ANN001
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

        def fake_popen_auth(args, **kwargs):  # noqa: ANN001
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(args[output_idx], returncode=1, stderr_lines=["auth required\n"])

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen_auth),
        ):
            runtime = build_codex_runtime(settings=settings)
            with self.assertRaises(CodexRuntimeError):
                runtime.run_text(system_prompt="s", user_prompt="u")

        def fake_popen_boom(args, **kwargs):  # noqa: ANN001
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(args[output_idx], returncode=2, stderr_lines=["boom\n"])

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen_boom),
        ):
            runtime = build_codex_runtime(settings=settings)
            with self.assertRaises(CodexRuntimeError):
                runtime.run_text(system_prompt="s", user_prompt="u")

        def fake_popen_empty(args, **kwargs):  # noqa: ANN001
            output_idx = args.index("--output-last-message") + 1
            return _FakePopen(args[output_idx], output_text="")

        with (
            patch("orchestrator.core.codex_runtime.shutil.which", return_value="/usr/bin/codex"),
            patch("orchestrator.core.codex_runtime.subprocess.Popen", side_effect=fake_popen_empty),
        ):
            runtime = build_codex_runtime(settings=settings)
            with self.assertRaises(CodexRuntimeError):
                runtime.run_text(system_prompt="s", user_prompt="u")


if __name__ == "__main__":
    unittest.main()
