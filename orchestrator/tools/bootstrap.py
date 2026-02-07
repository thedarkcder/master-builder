from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.storage.models import RepoBootstrapState


WORKFLOW_TEMPLATE_FILENAMES = ("ci.yml", "security.yml")
README_CI_MARKER = "## CI and Security Checks"
README_CI_NOTE = """## CI and Security Checks

This repository uses GitHub Actions workflow gates:
- `.github/workflows/ci.yml`
- `.github/workflows/security.yml`

Run local validation before opening PRs:
- `python3 -m unittest discover -s tests -p 'test_*.py'`
"""

SKILL_TEMPLATES: dict[str, str] = {
    "run_tests.md": """# Run tests

1. Run the repository's canonical test command(s).
2. Capture failing output clearly.
3. Fix failures and rerun until passing.
""",
    "add_tests.md": """# Add tests

1. For behavior changes, add or update tests in the same PR.
2. Prefer unit tests first, add integration tests when behavior crosses boundaries.
3. Keep test intent obvious from names and assertions.
""",
    "pr_checklist.md": """# PR checklist

- Summary of what changed
- How to test
- Risks and mitigations
- Rollback notes when relevant
""",
    "security_sanity.md": """# Security sanity checks

- No secrets, tokens, credentials, or private keys in code/logs
- No auth bypass or policy bypass
- No PII in logs, PR comments, or issue comments
""",
}

AGENTS_TEMPLATE = """# AGENTS

Read and follow `.codex/POLICY.md` before making code changes.
Use `.codex/OPERATING.md` for repository execution conventions.
"""

CODEX_REQUIRED_FILES = (
    ".codex/OPERATING.md",
    ".codex/POLICY.md",
    ".codex/ENGINEERING_STANDARDS.md",
    ".codex/DECISION_GATE_TEMPLATE.md",
    ".codex/skills/run_tests.md",
    ".codex/skills/add_tests.md",
    ".codex/skills/pr_checklist.md",
    ".codex/skills/security_sanity.md",
)


@dataclass(frozen=True)
class WorkflowBootstrapResult:
    copied_workflows: tuple[str, ...]
    readme_updated: bool


@dataclass(frozen=True)
class CodexBootstrapResult:
    created_files: tuple[str, ...]
    agents_created: bool
    already_bootstrapped: bool


@dataclass(frozen=True)
class RepoBootstrapStateResult:
    tenant_id: str
    repo_url: str
    bootstrap_count: int
    last_created_files: tuple[str, ...]
    bootstrapped_at: datetime
    updated_at: datetime


def _resolve_source_repo(template_repo_root: str | Path | None) -> Path:
    if template_repo_root is not None:
        return Path(template_repo_root).resolve()
    return Path(__file__).resolve().parents[2]


def bootstrap_ci_workflows(
    *,
    target_repo_dir: str | Path,
    template_repo_root: str | Path | None = None,
) -> WorkflowBootstrapResult:
    target_repo = Path(target_repo_dir).resolve()
    source_repo = _resolve_source_repo(template_repo_root)
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


def bootstrap_codex_assets(
    *,
    target_repo_dir: str | Path,
    template_repo_root: str | Path | None = None,
    create_agents_file: bool = True,
) -> CodexBootstrapResult:
    target_repo = Path(target_repo_dir).resolve()
    source_repo = _resolve_source_repo(template_repo_root)

    source_operating = source_repo / ".codex" / "OPERATING.md"
    source_policy = source_repo / ".codex" / "POLICY.md"
    source_engineering = source_repo / ".codex" / "ENGINEERING_STANDARDS.md"
    source_decision_gate = source_repo / ".codex" / "DECISION_GATE_TEMPLATE.md"
    if (
        not source_operating.exists()
        or not source_policy.exists()
        or not source_engineering.exists()
        or not source_decision_gate.exists()
    ):
        raise FileNotFoundError("Canonical .codex templates are missing required baseline docs")

    created_files: list[str] = []
    codex_dir = target_repo / ".codex"
    skills_dir = codex_dir / "skills"
    codex_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)

    mapping = {
        ".codex/OPERATING.md": source_operating.read_text(encoding="utf-8"),
        ".codex/POLICY.md": source_policy.read_text(encoding="utf-8"),
        ".codex/ENGINEERING_STANDARDS.md": source_engineering.read_text(encoding="utf-8"),
        ".codex/DECISION_GATE_TEMPLATE.md": source_decision_gate.read_text(encoding="utf-8"),
    }
    for skill_filename, skill_content in SKILL_TEMPLATES.items():
        mapping[f".codex/skills/{skill_filename}"] = skill_content

    for relative_path, content in mapping.items():
        destination = target_repo / relative_path
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        created_files.append(relative_path)

    agents_created = False
    agents_path = target_repo / "AGENTS.md"
    if create_agents_file and not agents_path.exists():
        agents_path.write_text(AGENTS_TEMPLATE, encoding="utf-8")
        created_files.append("AGENTS.md")
        agents_created = True

    already_bootstrapped = not created_files and has_required_codex_assets(target_repo)
    return CodexBootstrapResult(
        created_files=tuple(created_files),
        agents_created=agents_created,
        already_bootstrapped=already_bootstrapped,
    )


def has_required_codex_assets(target_repo_dir: str | Path) -> bool:
    target_repo = Path(target_repo_dir).resolve()
    for relative_path in CODEX_REQUIRED_FILES:
        if not (target_repo / relative_path).exists():
            return False
    return True


def load_codex_preflight_context(target_repo_dir: str | Path) -> dict[str, str]:
    target_repo = Path(target_repo_dir).resolve()
    policy_path = target_repo / ".codex" / "POLICY.md"
    operating_path = target_repo / ".codex" / "OPERATING.md"
    engineering_path = target_repo / ".codex" / "ENGINEERING_STANDARDS.md"
    decision_gate_path = target_repo / ".codex" / "DECISION_GATE_TEMPLATE.md"
    agents_path = target_repo / "AGENTS.md"

    if not policy_path.exists():
        raise FileNotFoundError("Missing required preflight file: .codex/POLICY.md")

    if agents_path.exists():
        operating_or_agents = agents_path.read_text(encoding="utf-8")
        operating_source = "AGENTS.md"
    elif operating_path.exists():
        operating_or_agents = operating_path.read_text(encoding="utf-8")
        operating_source = ".codex/OPERATING.md"
    else:
        raise FileNotFoundError("Missing required preflight file: AGENTS.md or .codex/OPERATING.md")

    if not engineering_path.exists():
        raise FileNotFoundError("Missing required preflight file: .codex/ENGINEERING_STANDARDS.md")
    if not decision_gate_path.exists():
        raise FileNotFoundError("Missing required preflight file: .codex/DECISION_GATE_TEMPLATE.md")

    return {
        "policy": policy_path.read_text(encoding="utf-8"),
        "operating_or_agents": operating_or_agents,
        "operating_source": operating_source,
        "engineering_standards": engineering_path.read_text(encoding="utf-8"),
        "decision_gate_template": decision_gate_path.read_text(encoding="utf-8"),
    }


def ensure_codex_bootstrap_state(
    *,
    session: Session,
    tenant_id: str,
    repo_url: str,
    target_repo_dir: str | Path,
    run_id: str | None = None,
    branch_name: str | None = None,
    template_repo_root: str | Path | None = None,
) -> CodexBootstrapResult:
    key = {"tenant_id": tenant_id, "repo_url": repo_url}
    existing = session.get(RepoBootstrapState, key)

    if existing is not None and has_required_codex_assets(target_repo_dir):
        existing.updated_at = datetime.now(timezone.utc)
        session.commit()
        return CodexBootstrapResult(created_files=(), agents_created=False, already_bootstrapped=True)

    bootstrap_result = bootstrap_codex_assets(
        target_repo_dir=target_repo_dir,
        template_repo_root=template_repo_root,
    )

    now = datetime.now(timezone.utc)
    if existing is None:
        bootstrap_count = 1
        bootstrapped_at = now
        state = RepoBootstrapState(
            tenant_id=tenant_id,
            repo_url=repo_url,
            last_branch=branch_name,
            last_run_id=run_id,
            last_created_files=list(bootstrap_result.created_files),
            bootstrap_count=bootstrap_count,
            bootstrapped_at=bootstrapped_at,
            updated_at=now,
        )
        session.add(state)
    else:
        existing.last_branch = branch_name
        existing.last_run_id = run_id
        existing.updated_at = now
        existing.last_created_files = list(bootstrap_result.created_files)
        if bootstrap_result.created_files:
            existing.bootstrap_count += 1
            existing.bootstrapped_at = now

    session.commit()
    return bootstrap_result


def list_repo_bootstrap_states(*, session: Session, tenant_id: str) -> list[RepoBootstrapStateResult]:
    states = session.execute(
        select(RepoBootstrapState)
        .where(RepoBootstrapState.tenant_id == tenant_id)
        .order_by(RepoBootstrapState.repo_url.asc())
    ).scalars().all()

    return [
        RepoBootstrapStateResult(
            tenant_id=state.tenant_id,
            repo_url=state.repo_url,
            bootstrap_count=state.bootstrap_count,
            last_created_files=tuple(state.last_created_files or []),
            bootstrapped_at=state.bootstrapped_at,
            updated_at=state.updated_at,
        )
        for state in states
    ]


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
