#!/usr/bin/env bash
set -euo pipefail

BASE_REF="${1:-origin/staging}"
DIFF_RANGE="${BASE_REF}...HEAD"
MANIFEST_PATH=".codex/codex_assets_manifest.json"
PYPROJECT_PATH="pyproject.toml"

if ! git rev-parse --verify "${BASE_REF}" >/dev/null 2>&1; then
  echo "Base ref '${BASE_REF}' is not available." >&2
  exit 1
fi

changed_files="$(git diff --name-only "${DIFF_RANGE}")"

assets_changed=false
manifest_changed=false

while IFS= read -r file; do
  [ -z "${file}" ] && continue
  case "${file}" in
    ".codex/POLICY.md"|\
    ".codex/ENGINEERING_STANDARDS.md"|\
    ".codex/OPERATING.md"|\
    ".codex/DECISION_GATE_TEMPLATE.md"|\
    ".codex/PR_READY_TEMPLATES.md"|\
    ".codex/policy_pack"*.json|\
    ".codex/codex_assets_manifest.json")
      assets_changed=true
      ;;
  esac
  if [ "${file}" = "${MANIFEST_PATH}" ]; then
    manifest_changed=true
  fi
done <<< "${changed_files}"

if [ "${assets_changed}" != "true" ]; then
  echo "No codex asset changes detected; version guard passed."
  exit 0
fi

if [ ! -f "${MANIFEST_PATH}" ]; then
  echo "Missing ${MANIFEST_PATH} in HEAD." >&2
  exit 1
fi

base_manifest_json="$(git show "${BASE_REF}:${MANIFEST_PATH}" 2>/dev/null || true)"
head_manifest_json="$(cat "${MANIFEST_PATH}")"

if [ -z "${head_manifest_json}" ]; then
  echo "Manifest ${MANIFEST_PATH} is empty in HEAD." >&2
  exit 1
fi

base_version="$(
  printf '%s' "${base_manifest_json}" | python3 -c 'import json,sys; raw=sys.stdin.read().strip(); print("" if not raw else json.loads(raw)["assets_version"])'
)"
head_version="$(
  printf '%s' "${head_manifest_json}" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["assets_version"])'
)"

if [ -z "${head_version}" ]; then
  echo "Manifest assets_version is empty in HEAD." >&2
  exit 1
fi

if [ "${manifest_changed}" = "true" ] && [ "${base_version}" = "${head_version}" ]; then
  echo "Manifest changed but assets_version did not change (${head_version})." >&2
  exit 1
fi

if [ "${assets_changed}" = "true" ] && [ "${base_version}" = "${head_version}" ]; then
  echo "Codex assets changed but assets_version was not bumped (still ${head_version})." >&2
  echo "Bump .codex/codex_assets_manifest.json -> assets_version." >&2
  exit 1
fi

if [ ! -f "${PYPROJECT_PATH}" ]; then
  echo "Missing ${PYPROJECT_PATH}; cannot validate pinned dependency." >&2
  exit 1
fi

pin_pattern="master-builder-codex-assets==${head_version}"
pin_matched=false
if command -v rg >/dev/null 2>&1; then
  if rg -q --fixed-strings "${pin_pattern}" "${PYPROJECT_PATH}"; then
    pin_matched=true
  fi
else
  if grep -Fq "${pin_pattern}" "${PYPROJECT_PATH}"; then
    pin_matched=true
  fi
fi

if [ "${pin_matched}" != "true" ]; then
  echo "Pinned dependency mismatch in ${PYPROJECT_PATH}." >&2
  echo "Expected: master-builder-codex-assets==${head_version}" >&2
  exit 1
fi

echo "Codex assets version guard passed (${base_version:-<none>} -> ${head_version})."
