#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
WORKER_CHECKOUT_DIR="${ROOT_DIR}/.workdirs"
mkdir -p "${WORKER_CHECKOUT_DIR}"

export ORCHESTRATOR_LIVE_VOICE_TRANSPORT_COMMAND="${ORCHESTRATOR_LIVE_VOICE_TRANSPORT_COMMAND:-/usr/local/bin/live-voice-transport}"
export ORCHESTRATOR_LIVE_VOICE_TRANSPORT_STARTUP_TIMEOUT_SECONDS="${ORCHESTRATOR_LIVE_VOICE_TRANSPORT_STARTUP_TIMEOUT_SECONDS:-10}"
export ORCHESTRATOR_LIVE_VOICE_TRANSPORT_REQUEST_TIMEOUT_SECONDS="${ORCHESTRATOR_LIVE_VOICE_TRANSPORT_REQUEST_TIMEOUT_SECONDS:-10}"
ADMIN_UI_PORT="${ADMIN_UI_PORT:-4100}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$(basename "$ROOT_DIR")}"
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

DOCKER_SERVICES=(
  postgres
  redis
  api
  run-worker
  webhook-worker
  deployment-host-bootstrap
  deployment-host-agent
  project-automation
  knowledge-sync
  discord-gateway
  discord-live-voice
  tailscale
)

current_service_container_id() {
  local service_name="$1"
  docker ps -a \
    --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME}" \
    --filter "label=com.docker.compose.service=${service_name}" \
    --format '{{.ID}}|{{.Status}}' \
    | awk -F'|' '$2 !~ /^Dead/ { print $1; exit }'
}

all_target_services_have_live_container() {
  local service_name
  for service_name in "${DOCKER_SERVICES[@]}"; do
    if [[ -z "$(current_service_container_id "$service_name")" ]]; then
      return 1
    fi
  done
  return 0
}

cleanup_dead_project_containers() {
  local dead_ids
  dead_ids="$(docker ps -a \
    --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME}" \
    --format '{{.ID}}|{{.Status}}' \
    | awk -F'|' '$2 ~ /^Dead/ { print $1 }')"
  if [[ -z "$dead_ids" ]]; then
    return 0
  fi
  echo "Removing stale dead Compose containers..."
  while IFS= read -r container_id; do
    [[ -z "$container_id" ]] && continue
    docker rm -f "$container_id" >/dev/null 2>&1 || true
  done <<< "$dead_ids"
}

wait_for_docker_services_ready() {
  local timeout_seconds="$1"
  local poll_seconds="$2"
  local start_ts
  start_ts="$(date +%s)"

  while true; do
    local all_ready="true"
    local service_name
    for service_name in "${DOCKER_SERVICES[@]}"; do
      local container_id
      local inspect_output
      local state_status
      local health_status

      container_id="$(current_service_container_id "$service_name")"
      if [[ -z "$container_id" ]]; then
        all_ready="false"
        continue
      fi

      inspect_output="$(docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || true)"
      state_status="${inspect_output%%|*}"
      health_status="${inspect_output##*|}"

      if [[ "$state_status" == "exited" || "$state_status" == "dead" ]]; then
        if [[ "$service_name" == "deployment-host-bootstrap" && "$state_status" == "exited" ]]; then
          if [[ "$health_status" == "none" ]]; then
            local exit_code
            exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$container_id" 2>/dev/null || true)"
            if [[ "$exit_code" == "0" ]]; then
              continue
            fi
          fi
        fi
        echo "Service '$service_name' container is not running (state=${state_status})."
        return 1
      fi

      if [[ "$service_name" == "postgres" || "$service_name" == "redis" || "$service_name" == "api" ]]; then
        if [[ "$health_status" != "healthy" ]]; then
          all_ready="false"
        fi
      elif [[ "$service_name" == "deployment-host-bootstrap" ]]; then
        if [[ "$state_status" == "running" ]]; then
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

    local now_ts
    now_ts="$(date +%s)"
    if (( now_ts - start_ts >= timeout_seconds )); then
      echo "Timed out waiting for Docker services readiness (${timeout_seconds}s)."
      return 1
    fi
    sleep "$poll_seconds"
  done
}

run_compose_up() {
  local output_file
  output_file="$(mktemp)"
  if docker compose up --build -d --remove-orphans "${DOCKER_SERVICES[@]}" 2>&1 | tee "$output_file"; then
    rm -f "$output_file"
    return 0
  fi

  local compose_exit_code="${PIPESTATUS[0]}"
  local compose_output
  compose_output="$(cat "$output_file")"
  rm -f "$output_file"

  if printf '%s' "$compose_output" | grep -q 'No such container:' && all_target_services_have_live_container; then
    echo "Compose reported stale container metadata; continuing with readiness checks."
    return 0
  fi
  return "$compose_exit_code"
}

admin_ui_listener_pids() {
  lsof -tiTCP:"${ADMIN_UI_PORT}" -sTCP:LISTEN 2>/dev/null || true
}

admin_ui_listener_cwd() {
  local pid="$1"
  lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | awk '/^n/ { sub(/^n/, ""); print; exit }'
}

local_worker_candidate_pids() {
  ps -axo pid=,command= | awk '/[[:space:]]-m orchestrator worker-runs([[:space:]]|$)/ { print $1 }'
}

local_worker_pids() {
  local candidate_pid
  local pid_cwd
  for candidate_pid in $(local_worker_candidate_pids); do
    [[ -z "$candidate_pid" || "$candidate_pid" == "$$" ]] && continue
    pid_cwd="$(admin_ui_listener_cwd "$candidate_pid")"
    if [[ "$pid_cwd" == "$ROOT_DIR" ]]; then
      printf '%s\n' "$candidate_pid"
    fi
  done
}

restart_existing_local_worker_if_owned() {
  local worker_pids
  worker_pids="$(local_worker_pids)"
  if [[ -z "$worker_pids" ]]; then
    return 0
  fi

  echo "Stopping existing local run worker..."
  local pid
  for pid in $worker_pids; do
    kill "$pid" >/dev/null 2>&1 || true
  done

  local deadline
  deadline="$(( $(date +%s) + 15 ))"
  while [[ -n "$(local_worker_pids)" ]]; do
    if (( $(date +%s) >= deadline )); then
      echo "Timed out waiting for existing local run worker to stop."
      return 1
    fi
    sleep 1
  done
  return 0
}

restart_existing_admin_ui_if_owned() {
  local listener_pids
  listener_pids="$(admin_ui_listener_pids)"
  if [[ -z "$listener_pids" ]]; then
    return 0
  fi

  local pid
  local pid_cwd
  for pid in $listener_pids; do
    pid_cwd="$(admin_ui_listener_cwd "$pid")"
    if [[ "$pid_cwd" != "${ROOT_DIR}/admin-ui" ]]; then
      echo "Port ${ADMIN_UI_PORT} is already in use by a non-admin-ui process (pid=${pid}, cwd=${pid_cwd:-unknown})."
      return 1
    fi
  done

  echo "Stopping existing admin UI on port ${ADMIN_UI_PORT}..."
  for pid in $listener_pids; do
    kill "$pid" >/dev/null 2>&1 || true
  done

  local deadline
  deadline="$(( $(date +%s) + 15 ))"
  while [[ -n "$(admin_ui_listener_pids)" ]]; do
    if (( $(date +%s) >= deadline )); then
      echo "Timed out waiting for admin UI port ${ADMIN_UI_PORT} to become free."
      return 1
    fi
    sleep 1
  done
  return 0
}

UI_PID=""
cleanup() {
  if [[ -n "${UI_PID}" ]] && kill -0 "${UI_PID}" >/dev/null 2>&1; then
    echo "Stopping local admin UI (pid=${UI_PID})..."
    kill "${UI_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

cleanup_dead_project_containers

run_compose_up

echo "Waiting for Docker services to become healthy/ready..."
wait_for_docker_services_ready "$DOCKER_WAIT_TIMEOUT_SECONDS" "$DOCKER_WAIT_INTERVAL_SECONDS"

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to start admin-ui."
  exit 1
fi

if [[ ! -f "${ROOT_DIR}/admin-ui/package.json" ]]; then
  echo "admin-ui/package.json not found."
  exit 1
fi

VENV_DIR="${ROOT_DIR}/.venv"
if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Creating virtual environment at ${VENV_DIR}..."
  python3 -m venv "${VENV_DIR}"
fi

export ORCHESTRATOR_DATABASE_URL="${ORCHESTRATOR_DATABASE_URL:-postgresql+psycopg://orchestrator:orchestrator@127.0.0.1:4402/orchestrator}"
export POSTGRES_URL="${POSTGRES_URL:-${ORCHESTRATOR_DATABASE_URL}}"
export ORCHESTRATOR_REDIS_URL="${ORCHESTRATOR_REDIS_URL:-redis://127.0.0.1:46379/0}"
export ORCHESTRATOR_WORKER_CAPABILITIES="${ORCHESTRATOR_WORKER_CAPABILITIES:-macos}"
export ORCHESTRATOR_AGENT_ID="${ORCHESTRATOR_AGENT_ID:-worker-macos-local}"
export ORCHESTRATOR_CODEX_SANDBOX_MODE="${ORCHESTRATOR_CODEX_SANDBOX_MODE:-danger-full-access}"
export REMOVED_PRIVATE_CREDENTIAL"${ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR:-${WORKER_CHECKOUT_DIR}}"

echo "Installing/updating Python dependencies in ${VENV_DIR}..."
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/pip" install -e .

cd "${ROOT_DIR}/admin-ui"
if [[ -n "$(admin_ui_listener_pids)" ]]; then
  restart_existing_admin_ui_if_owned
fi
echo "Starting local admin UI with npm run dev..."
npm run dev &
UI_PID="$!"
echo "Local admin UI started (pid=${UI_PID})"

cd "${ROOT_DIR}"
echo "Hybrid worker mode started."
echo "Docker worker capability: linux (container)"
echo "Local worker capability: ${ORCHESTRATOR_WORKER_CAPABILITIES}"
echo "Local Codex sandbox: ${ORCHESTRATOR_CODEX_SANDBOX_MODE}"
echo "Shared repo checkout dir: ${ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR}"
echo "Starting local run worker..."
restart_existing_local_worker_if_owned
"${VENV_DIR}/bin/python" -m orchestrator worker-runs
