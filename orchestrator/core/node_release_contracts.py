from __future__ import annotations

import json
import re

NODE_INSTALL_WITH_LEGACY_PEERS_COMMAND = "npm install --legacy-peer-deps"
NODE_INSTALL_WITH_LEGACY_PEERS_AND_PROGRESS_COMMAND = "npm install --legacy-peer-deps --loglevel=info"
REMOVE_YARN_LOCK_FOR_NPM_INSTALL_COMMAND = "rm -f yarn.lock"
LEGACY_NODE_INSTALL_WITH_PACKAGE_LOCK_FLAGS_COMMAND = (
    "npm install --package-lock=false --legacy-peer-deps --production=false"
)
LEGACY_EXPO_CLI_PACKAGE = "expo-cli@3.28.6"
LEGACY_EXPO_WEBSOCKET_PACKAGE = "websocket@1.0.35"
LEGACY_EXPO_RELEASE_ENVIRONMENT = {
    "NODE_ENV": "development",
    "NODE_OPTIONS": "--openssl-legacy-provider",
    "NPM_CONFIG_FETCH_RETRIES": "5",
    "NPM_CONFIG_FETCH_RETRY_FACTOR": "2",
    "NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT": "120000",
    "NPM_CONFIG_FETCH_RETRY_MINTIMEOUT": "10000",
    "NPM_CONFIG_NETWORK_TIMEOUT": "120000",
    "NPM_CONFIG_PRODUCTION": "false",
    "NPM_CONFIG_PYTHON": "/usr/bin/python3",
    "PYTHON": "/usr/bin/python3",
}
LEGACY_EXPO_NATIVE_BUILD_DEPS_COMMAND = (
    "sudo apt-get update && sudo apt-get install -y python3 make g++"
)
LEGACY_EXPO_NATIVE_BUILD_DEPS_COMMAND_WITHOUT_COOLIFY_LIMIT = (
    "sudo apt-get update && sudo apt-get install -y --no-install-recommends python3 make g++"
)
LEGACY_EXPO_WEB_START_COMMAND = (
    "NODE_OPTIONS=--openssl-legacy-provider npx expo-cli start --web --non-interactive --host lan"
)
LEGACY_EXPO_WEB_START_COMMAND_WITHOUT_OPENSSL = "npx expo-cli start --web --non-interactive --host lan"
INVALID_LEGACY_EXPO_WEB_START_COMMAND = "npx expo-cli start --web --non-interactive --host 0.0.0.0"
STALE_LEGACY_EXPO_WEB_START_COMMAND = f"{INVALID_LEGACY_EXPO_WEB_START_COMMAND} --port 19006"
LEGACY_EXPO_CLI_ADDON_INSTALL_COMMAND = (
    f"{NODE_INSTALL_WITH_LEGACY_PEERS_AND_PROGRESS_COMMAND} --no-save "
    f"{LEGACY_EXPO_CLI_PACKAGE} {LEGACY_EXPO_WEBSOCKET_PACKAGE}"
)
LEGACY_EXPO_CLI_INSTALL_COMMAND_WITHOUT_WEBSOCKET = (
    f"{LEGACY_NODE_INSTALL_WITH_PACKAGE_LOCK_FLAGS_COMMAND} && npm install --no-save --legacy-peer-deps "
    f"{LEGACY_EXPO_CLI_PACKAGE}"
)
LEGACY_EXPO_CLI_INSTALL_COMMAND_WITH_SEPARATE_EXPO_INSTALL = (
    f"{LEGACY_NODE_INSTALL_WITH_PACKAGE_LOCK_FLAGS_COMMAND} && npm install --no-save --legacy-peer-deps "
    f"{LEGACY_EXPO_CLI_PACKAGE} {LEGACY_EXPO_WEBSOCKET_PACKAGE}"
)
LEGACY_EXPO_CLI_INSTALL_COMMAND_WITHOUT_NATIVE_BUILD_TOOLS = (
    f"{LEGACY_NODE_INSTALL_WITH_PACKAGE_LOCK_FLAGS_COMMAND} --no-save {LEGACY_EXPO_CLI_PACKAGE} "
    f"{LEGACY_EXPO_WEBSOCKET_PACKAGE}"
)
LEGACY_EXPO_CLI_INSTALL_COMMAND_WITH_NATIVE_BUILD_TOOLS_WITHOUT_PROJECT_DEPS = (
    f"{LEGACY_EXPO_NATIVE_BUILD_DEPS_COMMAND_WITHOUT_COOLIFY_LIMIT} && "
    f"{LEGACY_EXPO_CLI_INSTALL_COMMAND_WITHOUT_NATIVE_BUILD_TOOLS}"
)
LEGACY_EXPO_CLI_INSTALL_COMMAND_OVERSIZED_WITH_PROJECT_DEPS = (
    f"{LEGACY_EXPO_NATIVE_BUILD_DEPS_COMMAND_WITHOUT_COOLIFY_LIMIT} && "
    f"{LEGACY_NODE_INSTALL_WITH_PACKAGE_LOCK_FLAGS_COMMAND} && "
    f"{LEGACY_NODE_INSTALL_WITH_PACKAGE_LOCK_FLAGS_COMMAND} --no-save "
    f"{LEGACY_EXPO_CLI_PACKAGE} {LEGACY_EXPO_WEBSOCKET_PACKAGE}"
)
LEGACY_EXPO_CLI_INSTALL_COMMAND_WITHOUT_NPM_LOG_PROGRESS = (
    f"{LEGACY_EXPO_NATIVE_BUILD_DEPS_COMMAND} && "
    f"{NODE_INSTALL_WITH_LEGACY_PEERS_COMMAND} && "
    f"npm install --legacy-peer-deps --no-save {LEGACY_EXPO_CLI_PACKAGE} {LEGACY_EXPO_WEBSOCKET_PACKAGE}"
)
LEGACY_EXPO_CLI_INSTALL_COMMAND_WITHOUT_YARN_LOCK_CLEANUP = (
    f"{LEGACY_EXPO_NATIVE_BUILD_DEPS_COMMAND} && "
    f"{NODE_INSTALL_WITH_LEGACY_PEERS_AND_PROGRESS_COMMAND} && "
    f"{LEGACY_EXPO_CLI_ADDON_INSTALL_COMMAND}"
)
LEGACY_EXPO_CLI_INSTALL_COMMAND = (
    f"{REMOVE_YARN_LOCK_FOR_NPM_INSTALL_COMMAND} && "
    f"{LEGACY_EXPO_NATIVE_BUILD_DEPS_COMMAND} && "
    f"{NODE_INSTALL_WITH_LEGACY_PEERS_AND_PROGRESS_COMMAND} && "
    f"{LEGACY_EXPO_CLI_ADDON_INSTALL_COMMAND}"
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
        "environment": dict(LEGACY_EXPO_RELEASE_ENVIRONMENT),
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
