from pathlib import Path

import pytest

from orchestrator.tools.bootstrap import (
    bootstrap_codex_assets,
    has_required_codex_assets,
)


ROOT = Path(__file__).resolve().parents[1]


def test_bootstrapped_guidance_preserves_exact_license_and_third_party_notices(
    tmp_path,
):
    result = bootstrap_codex_assets(target_repo_dir=tmp_path)
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        relative = f".codex/{name}"
        assert relative in result.created_files
        assert (tmp_path / relative).read_bytes() == (ROOT / relative).read_bytes()


def test_existing_bootstrapped_guidance_requires_missing_licensing_assets(tmp_path):
    bootstrap_codex_assets(target_repo_dir=tmp_path)
    (tmp_path / ".codex/LICENSE").unlink(missing_ok=True)
    (tmp_path / ".codex/THIRD_PARTY_NOTICES.md").unlink(missing_ok=True)
    assert not has_required_codex_assets(tmp_path)
    result = bootstrap_codex_assets(target_repo_dir=tmp_path)
    assert set(result.created_files) == {
        ".codex/LICENSE",
        ".codex/THIRD_PARTY_NOTICES.md",
    }
    assert has_required_codex_assets(tmp_path)


@pytest.mark.parametrize("missing", ["LICENSE", "THIRD_PARTY_NOTICES.md"])
def test_canonical_guidance_without_licensing_assets_fails_before_copying(
    tmp_path, missing
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    (source / ".codex").mkdir(parents=True)
    for name in (
        "OPERATING.md",
        "POLICY.md",
        "ENGINEERING_STANDARDS.md",
        "DECISION_GATE_TEMPLATE.md",
        "PR_READY_TEMPLATES.md",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
    ):
        if name != missing:
            (source / ".codex" / name).write_bytes(
                (ROOT / ".codex" / name).read_bytes()
            )
    with pytest.raises(FileNotFoundError, match=r"Canonical \.codex templates"):
        bootstrap_codex_assets(target_repo_dir=target, template_repo_root=source)
    assert not target.exists()
