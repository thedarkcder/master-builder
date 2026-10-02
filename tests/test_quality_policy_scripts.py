from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def _load_module(relative_path: str, module_name: str):
    module_path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_todo_marker_detects_inline_comment_without_issue_key() -> None:
    module = _load_module(
        "scripts/quality/check_no_todo_markers.py", "check_no_todo_markers"
    )
    assert module._has_untracked_todo_marker("value = 1  # TODO remove this")


def test_no_todo_marker_allows_tracked_issue_key() -> None:
    module = _load_module(
        "scripts/quality/check_no_todo_markers.py", "check_no_todo_markers"
    )
    assert not module._has_untracked_todo_marker(
        "value = 1  # TODO MAB-123 remove this"
    )


def test_no_todo_marker_ignores_plain_text_without_comment_token() -> None:
    module = _load_module(
        "scripts/quality/check_no_todo_markers.py", "check_no_todo_markers"
    )
    assert not module._has_untracked_todo_marker(
        "TODO appears in a user-visible string"
    )


def test_no_todo_scan_ignores_generated_browser_evidence(
    tmp_path, monkeypatch, capsys
) -> None:
    module = _load_module(
        "scripts/quality/check_no_todo_markers.py", "check_no_todo_markers"
    )
    root = tmp_path
    product = root / "admin-ui"
    product.mkdir()
    (product / "page.tsx").write_text("export const title = 'Example';\n")
    evidence = product / "test-results" / "recording.js"
    evidence.parent.mkdir()
    evidence.write_text("// TODO generated browser recording, not production code\n")
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "SCAN_ROOTS", (product,))
    assert module.main() == 0
    assert "No untracked" in capsys.readouterr().out


def test_changed_python_files_includes_rename_diff_filter(monkeypatch) -> None:
    module = _load_module(
        "scripts/quality/check_ruff_changed_lines.py", "check_ruff_changed_lines"
    )
    captured: dict[str, list[str]] = {}

    def _fake_run(cmd, **_kwargs):  # noqa: ANN001
        captured["cmd"] = list(cmd)
        return SimpleNamespace(stdout="orchestrator/core/example.py\n")

    monkeypatch.setattr(module.subprocess, "run", _fake_run)
    files = module._changed_python_files("main")
    assert "--diff-filter=AMR" in captured["cmd"]
    assert files == ["orchestrator/core/example.py"]
