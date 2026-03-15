#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
WORKER_CHECKOUT_DIR="${ROOT_DIR}/.workdirs"
mkdir -p "${WORKER_CHECKOUT_DIR}"
DOCKER_SERVICES=(postgres api worker knowledge-sync discord-gateway tailscale)
DOCKER_WAIT_TIMEOUT_SECONDS="${DOCKER_WAIT_TIMEOUT_SECONDS:-300}"
DOCKER_WAIT_INTERVAL_SECONDS="${DOCKER_WAIT_INTERVAL_SECONDS:-3}"

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

docker compose up --build -d "${DOCKER_SERVICES[@]}"

wait_for_docker_services_ready() {
  local timeout_seconds="$1"
  local poll_seconds="$2"
  local start_ts
  start_ts="$(date +%s)"

  while true; do
    local all_ready="true"
    for service in "${DOCKER_SERVICES[@]}"; do
      local container_id
      container_id="$(docker compose ps -q "$service" | head -n 1)"
      if [[ -z "$container_id" ]]; then
        all_ready="false"
        continue
      fi

      local inspect_output state_status health_status
      inspect_output="$(docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || true)"
      state_status="${inspect_output%%|*}"
      health_status="${inspect_output##*|}"

      if [[ "$state_status" == "exited" || "$state_status" == "dead" ]]; then
        echo "Service '$service' container is not running (state=${state_status})."
        docker compose ps "$service"
        return 1
      fi

      if [[ "$health_status" != "none" ]]; then
        if [[ "$health_status" != "healthy" ]]; then
          all_ready="false"
        fi
      elif [[ "$state_status" != "running" ]]; then
        all_ready="false"
      fi
    done

    if [[ "$all_ready" == "true" ]]; then
      echo "Docker services ready."
      return 0
    fi

    local now_ts elapsed
    now_ts="$(date +%s)"
    elapsed="$((now_ts - start_ts))"
    if (( elapsed >= timeout_seconds )); then
      echo "Timed out waiting for Docker services readiness (${timeout_seconds}s)."
      docker compose ps
      return 1
    fi
    sleep "$poll_seconds"
  done
}

echo "Waiting for Docker services to become healthy/ready..."
wait_for_docker_services_ready "${DOCKER_WAIT_TIMEOUT_SECONDS}" "${DOCKER_WAIT_INTERVAL_SECONDS}"

export ORCHESTRATOR_DATABASE_URL="${ORCHESTRATOR_DATABASE_URL:-postgresql+psycopg://orchestrator:orchestrator@127.0.0.1:4402/orchestrator}"
export POSTGRES_URL="${POSTGRES_URL:-${ORCHESTRATOR_DATABASE_URL}}"
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

UI_PID=""
cleanup() {
  if [[ -n "${UI_PID}" ]] && kill -0 "${UI_PID}" >/dev/null 2>&1; then
    echo "Stopping local admin UI (pid=${UI_PID})..."
    kill "${UI_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

if command -v npm >/dev/null 2>&1 && [[ -f "${ROOT_DIR}/admin-ui/package.json" ]]; then
  echo "Starting local admin UI with npm run dev (admin-ui)..."
  (
    cd "${ROOT_DIR}/admin-ui"
    npm run dev
  ) &
  UI_PID="$!"
  echo "Local admin UI started (pid=${UI_PID})"
else
  echo "Skipping local admin UI startup (npm or admin-ui/package.json not found)."
fi

echo "Hybrid worker mode started."
echo "Docker worker capability: linux (container)"
echo "Local worker capability: ${ORCHESTRATOR_WORKER_CAPABILITIES}"
echo "Local Codex sandbox: ${ORCHESTRATOR_CODEX_SANDBOX_MODE}"
echo "Shared repo checkout dir: ${ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR}"
echo "Starting local worker..."

"${VENV_DIR}/bin/python" -m orchestrator worker
