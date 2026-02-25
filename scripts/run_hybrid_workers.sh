#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
WORKER_CHECKOUT_DIR="${ROOT_DIR}/.workdirs"
mkdir -p "${WORKER_CHECKOUT_DIR}"

if [[ -f ".env" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      value="${BASH_REMATCH[2]}"
      if [[ "$value" =~ ^\"(.*)\"$ ]]; then
        value="${BASH_REMATCH[1]}"
      elif [[ "$value" =~ ^\'(.*)\'$ ]]; then
        value="${BASH_REMATCH[1]}"
      fi
      export "${key}=${value}"
    fi
  done < .env
fi

docker compose up --build -d postgres api worker discord-gateway tailscale

export ORCHESTRATOR_DATABASE_URL="${ORCHESTRATOR_DATABASE_URL:-postgresql+psycopg://orchestrator:orchestrator@127.0.0.1:4402/orchestrator}"
export ORCHESTRATOR_WORKER_CAPABILITIES="${ORCHESTRATOR_WORKER_CAPABILITIES:-macos}"
export ORCHESTRATOR_AGENT_ID="${ORCHESTRATOR_AGENT_ID:-worker-macos-local}"
export ORCHESTRATOR_CODEX_SANDBOX_MODE="${ORCHESTRATOR_CODEX_SANDBOX_MODE:-danger-full-access}"
export REMOVED_PRIVATE_CREDENTIAL"${ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR:-${WORKER_CHECKOUT_DIR}}"

VENV_DIR="${ROOT_DIR}/.venv"
if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Creating virtual environment at ${VENV_DIR}..."
  python3 -m venv "${VENV_DIR}"
fi

echo "Installing/updating Python dependencies in ${VENV_DIR}..."
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/pip" install -e .

echo "Hybrid worker mode started."
echo "Docker worker capability: linux (container)"
echo "Local worker capability: ${ORCHESTRATOR_WORKER_CAPABILITIES}"
echo "Local Codex sandbox: ${ORCHESTRATOR_CODEX_SANDBOX_MODE}"
echo "Shared repo checkout dir: ${ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR}"
echo "Starting local worker..."

"${VENV_DIR}/bin/python" -m orchestrator worker
