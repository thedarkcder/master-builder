from __future__ import annotations

import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from orchestrator.core.runtime.runtime_home import prepare_runtime_home, resolve_runtime_home


class CodexRuntimeHomeTests(TestCase):
    def _settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            agent_id="worker-macos-local",
            runtime_home="",
        )

    def test_resolve_runtime_home_uses_stable_local_root(self) -> None:
        settings = self._settings()
        with TemporaryDirectory() as temp_dir:
            with (
                patch.dict(os.environ, {"HOME": temp_dir}, clear=False),
                patch("orchestrator.core.runtime.runtime_home._running_inside_container", return_value=False),
            ):
                runtime_home = resolve_runtime_home(settings=settings)

        self.assertEqual(
            runtime_home,
            Path(temp_dir) / ".master-builder" / "runtime" / "worker-macos-local",
        )

    def test_resolve_runtime_home_uses_container_volume_root(self) -> None:
        settings = self._settings()
        with TemporaryDirectory() as temp_dir:
            with (
                patch.dict(os.environ, {"HOME": temp_dir}, clear=False),
                patch("orchestrator.core.runtime.runtime_home._running_inside_container", return_value=True),
            ):
                runtime_home = resolve_runtime_home(settings=settings)

        self.assertEqual(
            runtime_home,
            Path(temp_dir) / ".codex" / "runtime" / "worker-macos-local",
        )

    def test_prepare_runtime_home_migrates_legacy_state_and_syncs_repo_assets(self) -> None:
        settings = self._settings()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = temp_path / "repo"
            repo_codex = repo_root / ".codex"
            (repo_codex / "skills").mkdir(parents=True, exist_ok=True)
            (repo_codex / "config.toml").write_text("model = 'gpt-5.4'\n", encoding="utf-8")
            (repo_codex / "skills" / "triage.md").write_text("repo managed skill\n", encoding="utf-8")

            home_root = temp_path / "home"
            legacy_runtime_home = repo_root / ".runtime-home" / "codex_cli"
            (legacy_runtime_home / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
            (legacy_runtime_home / ".codex" / "auth.json").write_text('{"access":"legacy"}', encoding="utf-8")
            (legacy_runtime_home / ".codex" / "sessions" / "session.json").write_text(
                '{"session":"legacy"}',
                encoding="utf-8",
            )
            (legacy_runtime_home / ".config").mkdir(parents=True, exist_ok=True)
            (legacy_runtime_home / ".config" / "settings.json").write_text('{"theme":"legacy"}', encoding="utf-8")

            with (
                patch.dict(os.environ, {"HOME": str(home_root)}, clear=False),
                patch("orchestrator.core.runtime.runtime_home._running_inside_container", return_value=False),
                patch("orchestrator.core.runtime.runtime_home._repo_root", return_value=repo_root),
                patch("orchestrator.core.runtime.runtime_home._repo_codex_dir", return_value=repo_codex),
            ):
                runtime_home = prepare_runtime_home(settings=settings, runtime_kind="codex_cli")

                target_codex = runtime_home / ".codex"
                self.assertEqual(
                    runtime_home,
                    home_root / ".master-builder" / "runtime" / "worker-macos-local",
                )
                self.assertTrue((target_codex / "config.toml").is_file())
                self.assertEqual(
                    (target_codex / "auth.json").read_text(encoding="utf-8"),
                    '{"access":"legacy"}',
                )
                self.assertEqual(
                    (target_codex / "sessions" / "session.json").read_text(encoding="utf-8"),
                    '{"session":"legacy"}',
                )
                self.assertEqual(
                    (runtime_home / ".config" / "settings.json").read_text(encoding="utf-8"),
                    '{"theme":"legacy"}',
                )

                # Preserve runtime-owned auth while pruning removed repo-managed files on resync.
                (repo_codex / "skills" / "triage.md").unlink()
                (target_codex / "skills" / "local-only.md").write_text("runtime-owned local file\n", encoding="utf-8")
                prepare_runtime_home(settings=settings, runtime_kind="codex_cli")

                self.assertFalse((target_codex / "skills" / "triage.md").exists())
                self.assertTrue((target_codex / "skills" / "local-only.md").is_file())
                self.assertEqual(
                    (target_codex / "auth.json").read_text(encoding="utf-8"),
                    '{"access":"legacy"}',
                )

    def test_prepare_runtime_home_ignores_disappearing_legacy_runtime_files(self) -> None:
        settings = self._settings()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_root = temp_path / "repo"
            repo_codex = repo_root / ".codex"
            repo_codex.mkdir(parents=True, exist_ok=True)
            (repo_codex / "config.toml").write_text("model = 'gpt-5.4'\n", encoding="utf-8")

            home_root = temp_path / "home"
            legacy_shared_codex = home_root / ".codex"
            (legacy_shared_codex / "sessions").mkdir(parents=True, exist_ok=True)
            (legacy_shared_codex / "auth.json").write_text('{"access":"legacy"}', encoding="utf-8")
            disappearing_session = legacy_shared_codex / "sessions" / "volatile.json"
            disappearing_session.write_text('{"session":"volatile"}', encoding="utf-8")

            real_copy2 = shutil.copy2

            def _copy2_with_race(source, target, *args, **kwargs):  # noqa: ANN001
                if Path(source) == disappearing_session:
                    disappearing_session.unlink(missing_ok=True)
                    raise FileNotFoundError(source)
                return real_copy2(source, target, *args, **kwargs)

            with (
                patch.dict(os.environ, {"HOME": str(home_root)}, clear=False),
                patch("orchestrator.core.runtime.runtime_home._running_inside_container", return_value=True),
                patch("orchestrator.core.runtime.runtime_home._repo_root", return_value=repo_root),
                patch("orchestrator.core.runtime.runtime_home._repo_codex_dir", return_value=repo_codex),
                patch("orchestrator.core.runtime.runtime_home.shutil.copy2", side_effect=_copy2_with_race),
            ):
                runtime_home = prepare_runtime_home(settings=settings, runtime_kind="codex_cli")

            target_codex = runtime_home / ".codex"
            self.assertEqual(
                (target_codex / "auth.json").read_text(encoding="utf-8"),
                '{"access":"legacy"}',
            )
            self.assertFalse((target_codex / "sessions" / "volatile.json").exists())
