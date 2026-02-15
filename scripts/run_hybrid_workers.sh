#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f ".env" ]]; then
  # shellcheck disable=SC1091
  source .env
fi

docker compose up -d postgres packages api worker admin-ui

export ORCHESTRATOR_DATABASE_URL="${ORCHESTRATOR_DATABASE_URL:-postgresql+psycopg://orchestrator:orchestrator@127.0.0.1:4402/orchestrator}"
export ORCHESTRATOR_WORKER_CAPABILITIES="${ORCHESTRATOR_WORKER_CAPABILITIES:-macos}"
export ORCHESTRATOR_AGENT_ID="${ORCHESTRATOR_AGENT_ID:-worker-macos-local}"

echo "Hybrid worker mode started."
echo "Docker worker capability: linux (container)"
echo "Local worker capability: ${ORCHESTRATOR_WORKER_CAPABILITIES}"
echo "Starting local worker..."

python -m orchestrator worker
