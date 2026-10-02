import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_codex_runtime_image_uses_exact_release_and_validates_installed_version():
    dockerfile = (ROOT / "orchestrator/Dockerfile").read_text()
    match = re.search(
        r"^ARG CODEX_NPM_VERSION=(\d+\.\d+\.\d+)$", dockerfile, re.MULTILINE
    )
    assert match is not None, (
        "Codex runtime must use an exact stable release, never a distribution tag"
    )
    assert 'test "$(codex --version)" = "codex-cli ${CODEX_NPM_VERSION}"' in dockerfile
    assert "CODEX_NPM_VERSION must be an exact stable release" in dockerfile


def test_compose_runtime_tool_defaults_do_not_override_exact_image_pin():
    for relative in ("docker-compose.yml", "deploy/hetzner/docker-compose.prod.yml"):
        compose = (ROOT / relative).read_text()
        values = re.findall(
            r"CODEX_NPM_VERSION:\s*\$\{CODEX_NPM_VERSION:-([^}]+)\}", compose
        )
        assert values, f"No Codex runtime build arguments inspected in {relative}"
        assert all(re.fullmatch(r"\d+\.\d+\.\d+", value) for value in values), relative


def test_compose_native_voice_has_one_source_build_contract():
    for relative in ("docker-compose.yml", "deploy/hetzner/docker-compose.prod.yml"):
        compose = (ROOT / relative).read_text()
        assert "LIBDAVE_FORCE_BUILD" not in compose, (
            f"{relative} must not expose an obsolete alternative native build mode"
        )
