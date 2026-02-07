#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

OUTPUT_DIR="${1:-${REPO_ROOT}/dist/codex-assets}"
BUILD_ROOT="$(mktemp -d)"
trap 'rm -rf "${BUILD_ROOT}"' EXIT

MANIFEST_ASSETS_VERSION="$(
  python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["assets_version"])' \
    "${REPO_ROOT}/.codex/codex_assets_manifest.json"
)"
ASSETS_VERSION="${CODEX_ASSETS_PACKAGE_VERSION:-${MANIFEST_ASSETS_VERSION}}"

PACKAGE_ROOT="${BUILD_ROOT}/master-builder-codex-assets"
MODULE_ROOT="${PACKAGE_ROOT}/src/master_builder_codex_assets"

mkdir -p "${MODULE_ROOT}"

cp "${REPO_ROOT}/.codex/POLICY.md" "${MODULE_ROOT}/POLICY.md"
cp "${REPO_ROOT}/.codex/ENGINEERING_STANDARDS.md" "${MODULE_ROOT}/ENGINEERING_STANDARDS.md"
cp "${REPO_ROOT}/.codex/OPERATING.md" "${MODULE_ROOT}/OPERATING.md"
cp "${REPO_ROOT}/.codex/DECISION_GATE_TEMPLATE.md" "${MODULE_ROOT}/DECISION_GATE_TEMPLATE.md"
cp "${REPO_ROOT}/.codex/PR_READY_TEMPLATES.md" "${MODULE_ROOT}/PR_READY_TEMPLATES.md"
cp "${REPO_ROOT}/.codex/codex_assets_manifest.json" "${MODULE_ROOT}/codex_assets_manifest.json"
cp "${REPO_ROOT}/.codex/DECISION_GATE_TEMPLATE.md" "${MODULE_ROOT}/decision_gate.md"
cp "${REPO_ROOT}/.codex/policy_pack"*.json "${MODULE_ROOT}/"

cat > "${MODULE_ROOT}/__init__.py" <<EOF
"""Versioned codex assets package."""

__version__ = "${ASSETS_VERSION}"
EOF

cat > "${PACKAGE_ROOT}/README.md" <<EOF
# master-builder-codex-assets

Versioned codex policy, operating standards, decision-gate templates, and policy packs
for the MASTER-BUILDER orchestrator runtime.
EOF

cat > "${PACKAGE_ROOT}/pyproject.toml" <<EOF
[build-system]
requires = ["setuptools>=69", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "master-builder-codex-assets"
version = "${ASSETS_VERSION}"
description = "Versioned codex assets for MASTER-BUILDER runtime enforcement"
readme = "README.md"
requires-python = ">=3.11"

[tool.setuptools]
package-dir = {"" = "src"}

[tool.setuptools.packages.find]
where = ["src"]
include = ["master_builder_codex_assets*"]

[tool.setuptools.package-data]
master_builder_codex_assets = ["*.md", "*.json"]
EOF

mkdir -p "${OUTPUT_DIR}"
python3 -m build --no-isolation "${PACKAGE_ROOT}" --outdir "${OUTPUT_DIR}"

echo "Built codex assets package version ${ASSETS_VERSION} in ${OUTPUT_DIR}"
