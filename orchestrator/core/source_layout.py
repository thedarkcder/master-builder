"""Validate the operational assets required by source-based installations."""

from pathlib import Path

_REQUIRED_FILES = (
    "pyproject.toml",
    "uv.lock",
    "alembic.ini",
    ".codex/config.toml",
    ".codex/POLICY.md",
    ".codex/ENGINEERING_STANDARDS.md",
    ".codex/OPERATING.md",
    "orchestrator/storage/migrations/env.py",
    "scripts/bootstrap_deployment_host_agent.py",
    "scripts/qa_demo_android_recorder.py",
    "scripts/qa_demo_mobile_recorder.py",
    "scripts/qa_demo_release_context.py",
    "scripts/qa_demo_recorder.mjs",
)


def require_source_checkout(*, root: Path | None = None) -> Path:
    """Return the single operational source root, or fail before runtime work."""
    source_root = (
        Path(__file__).resolve().parents[2] if root is None else root.resolve()
    )
    missing = [
        relative
        for relative in _REQUIRED_FILES
        if not (source_root / relative).is_file()
    ]
    if missing:
        raise RuntimeError(
            "Master Builder requires a full source checkout or a runtime image built from this repository; "
            "a standalone wheel installation is not supported. Missing operational assets: "
            + ", ".join(missing)
            + ". Restore the complete checkout and run uv sync --frozen --extra dev from its root."
        )
    return source_root
