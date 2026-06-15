from __future__ import annotations

import json
import re

NODE_INSTALL_WITH_LEGACY_PEERS_COMMAND = "npm install --package-lock=false --legacy-peer-deps --production=false"
LEGACY_EXPO_CLI_PACKAGE = "expo-cli@3.28.6"
LEGACY_EXPO_WEB_START_COMMAND = "npx expo-cli start --web --non-interactive --host 0.0.0.0 --port 19006"
LEGACY_EXPO_CLI_INSTALL_COMMAND = (
    f"{NODE_INSTALL_WITH_LEGACY_PEERS_COMMAND} && npm install --no-save --legacy-peer-deps {LEGACY_EXPO_CLI_PACKAGE}"
)


def legacy_expo_web_release_contract(package_json_text: str) -> dict[str, str]:
    try:
        payload = json.loads(package_json_text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    scripts = payload.get("scripts") if isinstance(payload.get("scripts"), dict) else {}
    web_script = scripts.get("web") if isinstance(scripts, dict) else None
    if not isinstance(web_script, str) or "expo start" not in web_script or "--web" not in web_script:
        return {}
    dependencies: dict[str, object] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        raw = payload.get(key)
        if isinstance(raw, dict):
            dependencies.update(raw)
    expo_major = _dependency_major(_dependency_version(dependencies, "expo"))
    has_explicit_expo_cli = "expo-cli" in dependencies
    if expo_major is None or expo_major >= 46 or has_explicit_expo_cli:
        return {}
    return {
        "install_command": LEGACY_EXPO_CLI_INSTALL_COMMAND,
        "start_command": LEGACY_EXPO_WEB_START_COMMAND,
    }


def _dependency_version(dependencies: dict[str, object], package_name: str) -> str | None:
    value = dependencies.get(package_name)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _dependency_major(version: str | None) -> int | None:
    if version is None:
        return None
    match = re.search(r"(\d+)", version)
    if match is None:
        return None
    return int(match.group(1))
