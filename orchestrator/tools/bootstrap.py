from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


WORKFLOW_TEMPLATE_FILENAMES = ("ci.yml", "security.yml")
README_CI_MARKER = "## CI and Security Checks"
README_CI_NOTE = """## CI and Security Checks

This repository uses GitHub Actions workflow gates:
- `.github/workflows/ci.yml`
- `.github/workflows/security.yml`

Run local validation before opening PRs:
- `python3 -m unittest discover -s tests -p 'test_*.py'`
"""


@dataclass(frozen=True)
class WorkflowBootstrapResult:
    copied_workflows: tuple[str, ...]
    readme_updated: bool


def bootstrap_ci_workflows(
    *,
    target_repo_dir: str | Path,
    template_repo_root: str | Path | None = None,
) -> WorkflowBootstrapResult:
    target_repo = Path(target_repo_dir).resolve()
    source_repo = (
        Path(template_repo_root).resolve()
        if template_repo_root is not None
        else Path(__file__).resolve().parents[2]
    )
    source_workflows = source_repo / ".github" / "workflows"
    target_workflows = target_repo / ".github" / "workflows"
    target_workflows.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for filename in WORKFLOW_TEMPLATE_FILENAMES:
        source_file = source_workflows / filename
        if not source_file.exists():
            raise FileNotFoundError(f"Workflow template is missing: {source_file}")

        destination_file = target_workflows / filename
        if destination_file.exists():
            continue

        shutil.copy2(source_file, destination_file)
        copied.append(str(destination_file.relative_to(target_repo)))

    readme_updated = _ensure_readme_ci_note(target_repo)
    return WorkflowBootstrapResult(copied_workflows=tuple(copied), readme_updated=readme_updated)


def _ensure_readme_ci_note(target_repo: Path) -> bool:
    readme_path = target_repo / "README.md"
    if not readme_path.exists():
        return False

    original = readme_path.read_text(encoding="utf-8")
    if README_CI_MARKER in original:
        return False

    separator = "\n" if original.endswith("\n") else "\n\n"
    updated = f"{original}{separator}{README_CI_NOTE}"
    readme_path.write_text(updated, encoding="utf-8")
    return True
