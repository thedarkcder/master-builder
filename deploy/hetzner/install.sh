#!/usr/bin/env bash
set -euo pipefail

LAUNCH_URL="${MASTER_BUILDER_LAUNCH_URL:-https://raw.githubusercontent.com/your-org/master-builder/main/deploy/hetzner/launch.sh}"

if ! command -v curl >/dev/null 2>&1; then
  echo "missing required command: curl" >&2
  exit 1
fi

tmp_script="$(mktemp)"
cleanup() {
  rm -f "${tmp_script}"
}
trap cleanup EXIT

curl -fsSL "${LAUNCH_URL}" -o "${tmp_script}"
chmod +x "${tmp_script}"
bash "${tmp_script}" "$@"
