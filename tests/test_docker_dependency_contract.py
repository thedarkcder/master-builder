import json
import re
import shlex
from pathlib import Path

import pytest


DOCKERFILE = Path(__file__).resolve().parents[1] / "orchestrator/Dockerfile"


def _stages():
    instructions = re.sub(r"\\\n\s*", " ", DOCKERFILE.read_text()).splitlines()
    stages = {}
    current = None
    for instruction in instructions:
        tokens = shlex.split(instruction)
        if tokens and tokens[0] == "FROM":
            current = tokens[-1]
            stages[current] = []
        elif current is not None and tokens:
            stages[current].append(tokens)
    return stages


def test_runtime_dependency_layer_uses_repository_lock_and_pinned_installer():
    base = _stages()["python-common-base"]
    assert any(
        tokens[0] == "COPY" and {"pyproject.toml", "uv.lock"} <= set(tokens)
        for tokens in base
    ), "Runtime dependency installation must receive the committed lockfile"
    assert any(
        tokens[0] == "COPY"
        and "--from=ghcr.io/astral-sh/uv:0.9.24" in tokens
        and "/uv" in tokens
        for tokens in base
    ), "The build must use the contributor-tested uv version"
    assert ["ENV", "UV_PROJECT_ENVIRONMENT=/opt/venv"] in base


def test_runtime_browser_uses_contributor_tested_playwright_version():
    lock = json.loads(
        (DOCKERFILE.parents[1] / "admin-ui/package-lock.json").read_text()
    )
    version = lock["packages"]["node_modules/playwright"]["version"]
    base = _stages()["python-common-base"]
    assert ["ARG", f"PLAYWRIGHT_NPM_VERSION={version}"] in base, (
        "Runtime browser installation must match the tested browser toolchain"
    )


@pytest.mark.parametrize(
    ("stage", "voice"), [("python-common-base", False), ("voice-runtime", True)]
)
def test_runtime_dependency_installation_is_frozen_without_development_extras(
    stage, voice
):
    instructions = _stages()[stage]
    commands = [tokens for tokens in instructions if tokens[0] == "RUN"]
    installs = [tokens for tokens in commands if "sync" in tokens and "uv" in tokens]
    assert len(installs) == 1, (
        f"{stage} must install its Python dependencies from uv.lock"
    )
    install = installs[0]
    assert {"--frozen", "--no-dev", "--no-install-project"} <= set(install)
    assert ("--extra" in install) == voice
    if voice:
        assert install[install.index("--extra") + 1] == "voice"
    assert not any(
        "pip" in token and "install" in tokens
        for tokens in commands
        for token in tokens
    )
