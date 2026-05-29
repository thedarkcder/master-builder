#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
WORKER_CHECKOUT_DIR="${ROOT_DIR}/.workdirs"
mkdir -p "${WORKER_CHECKOUT_DIR}"
HYBRID_CACHE_DIR="${WORKER_CHECKOUT_DIR}/.hybrid-worker"
mkdir -p "${HYBRID_CACHE_DIR}"

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

export ORCHESTRATOR_LIVE_VOICE_TRANSPORT_COMMAND="${ORCHESTRATOR_LIVE_VOICE_TRANSPORT_COMMAND:-/usr/local/bin/live-voice-transport}"
export ORCHESTRATOR_LIVE_VOICE_TRANSPORT_STARTUP_TIMEOUT_SECONDS="${ORCHESTRATOR_LIVE_VOICE_TRANSPORT_STARTUP_TIMEOUT_SECONDS:-10}"
export ORCHESTRATOR_LIVE_VOICE_TRANSPORT_REQUEST_TIMEOUT_SECONDS="${ORCHESTRATOR_LIVE_VOICE_TRANSPORT_REQUEST_TIMEOUT_SECONDS:-10}"
export ADMIN_UI_PORT="${ADMIN_UI_PORT:-${MASTER_BUILDER_ADMIN_UI_PORT:-60002}}"
export MASTER_BUILDER_API_PORT="${MASTER_BUILDER_API_PORT:-60001}"
export MASTER_BUILDER_POSTGRES_PORT="${MASTER_BUILDER_POSTGRES_PORT:-60003}"
export MASTER_BUILDER_CLICKHOUSE_HTTP_PORT="${MASTER_BUILDER_CLICKHOUSE_HTTP_PORT:-60006}"
export MASTER_BUILDER_OTEL_HTTP_PORT="${MASTER_BUILDER_OTEL_HTTP_PORT:-60010}"
LOCAL_PUBLIC_API_BASE_URL="http://localhost:${MASTER_BUILDER_API_PORT}"
LOCAL_ADMIN_UI_BASE_URL="http://localhost:${ADMIN_UI_PORT}"
LOCAL_DATABASE_URL="postgresql+psycopg://orchestrator:orchestrator@127.0.0.1:${MASTER_BUILDER_POSTGRES_PORT}/orchestrator"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$(basename "$ROOT_DIR")}"
DOCKER_WAIT_TIMEOUT_SECONDS="${DOCKER_WAIT_TIMEOUT_SECONDS:-300}"
DOCKER_WAIT_INTERVAL_SECONDS="${DOCKER_WAIT_INTERVAL_SECONDS:-3}"
HYBRID_DOCKER_BUILD_MODE="${HYBRID_DOCKER_BUILD_MODE:-auto}"
HYBRID_LOCAL_PYTHON_INSTALL_MODE="${HYBRID_LOCAL_PYTHON_INSTALL_MODE:-auto}"
DOCKER_BUILD_FINGERPRINT_FILE="${HYBRID_CACHE_DIR}/docker-build.sha256"
LOCAL_PYTHON_INSTALL_FINGERPRINT_FILE="${HYBRID_CACHE_DIR}/local-python-install.sha256"
DOCKER_DATABASE_URL="${MASTER_BUILDER_DOCKER_DATABASE_URL:-postgresql+psycopg://orchestrator:orchestrator@postgres:5432/orchestrator}"

docker_compose() {
  ORCHESTRATOR_DATABASE_URL="$DOCKER_DATABASE_URL" POSTGRES_URL="$DOCKER_DATABASE_URL" docker compose "$@"
}

DOCKER_BASE_SERVICES=(
  postgres
  mailpit
  clickhouse
  temporal
  tempo
  otel-collector
)

DOCKER_BASE_RUNTIME_SERVICES=(
  api
  run-worker
  project-automation
  knowledge-sync
  temporal-orchestrator
)

DOCKER_POST_API_SERVICES=(
  tailscale
)

DOCKER_BASE_APP_SERVICES=(
  api
  run-worker
  project-automation
  knowledge-sync
  temporal-orchestrator
)

DOCKER_VOICE_SERVICES=(
  webhook-worker
  discord-gateway
  discord-live-voice
)

DOCKER_STOP_BEFORE_MIGRATION_SERVICES=(
  "${DOCKER_BASE_APP_SERVICES[@]}"
  "${DOCKER_VOICE_SERVICES[@]}"
  "${DOCKER_POST_API_SERVICES[@]}"
)

DOCKER_RUNTIME_SERVICES=("${DOCKER_BASE_RUNTIME_SERVICES[@]}" "${DOCKER_VOICE_SERVICES[@]}")
DOCKER_APP_SERVICES=("${DOCKER_BASE_APP_SERVICES[@]}" "${DOCKER_VOICE_SERVICES[@]}")

DOCKER_READY_SERVICES=(
  "${DOCKER_BASE_SERVICES[@]}"
  "${DOCKER_RUNTIME_SERVICES[@]}"
  "${DOCKER_POST_API_SERVICES[@]}"
)

DOCKER_BUILD_SERVICES=(
  migrate
  "${DOCKER_APP_SERVICES[@]}"
)

sha256_stream() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{ print $1 }'
    return 0
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{ print $1 }'
    return 0
  fi
  echo "Neither shasum nor sha256sum is available." >&2
  return 1
}

sha256_file() {
  local file_path="$1"
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file_path" | awk '{ print $1 }'
    return 0
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file_path" | awk '{ print $1 }'
    return 0
  fi
  echo "Neither shasum nor sha256sum is available." >&2
  return 1
}

fingerprint_paths() {
  local path
  for path in "$@"; do
    if [[ -f "$path" ]]; then
      printf 'file %s %s\n' "$path" "$(sha256_file "$path")"
    elif [[ -d "$path" ]]; then
      find "$path" -type f \
        ! -path '*/__pycache__/*' \
        ! -name '*.pyc' \
        | LC_ALL=C sort \
        | while IFS= read -r file_path; do
            printf 'file %s %s\n' "$file_path" "$(sha256_file "$file_path")"
          done
    else
      printf 'missing %s\n' "$path"
    fi
  done | sha256_stream
}

docker_build_fingerprint() {
  local fingerprint_input
  fingerprint_input="$(
    printf 'CODEX_NPM_VERSION=%s\n' "${CODEX_NPM_VERSION:-latest}"
    fingerprint_paths \
      docker-compose.yml \
      orchestrator/Dockerfile \
      .dockerignore \
      pyproject.toml \
      README.md \
      alembic.ini \
      .codex \
      orchestrator
    fingerprint_paths discord_live_voice_transport
  )"
  printf '%s\n' "$fingerprint_input" | sha256_stream
}

docker_build_required() {
  case "$HYBRID_DOCKER_BUILD_MODE" in
    always)
      return 0
      ;;
    never)
      return 1
      ;;
    auto)
      local current_fingerprint
      current_fingerprint="$(docker_build_fingerprint)"
      if [[ ! -f "$DOCKER_BUILD_FINGERPRINT_FILE" ]]; then
        if docker_build_images_available; then
          echo "Docker build fingerprint missing but Compose images exist; seeding fingerprint without rebuilding."
          printf '%s\n' "$current_fingerprint" > "$DOCKER_BUILD_FINGERPRINT_FILE"
          return 1
        fi
        return 0
      fi
      [[ "$(cat "$DOCKER_BUILD_FINGERPRINT_FILE")" != "$current_fingerprint" ]]
      ;;
    *)
      echo "HYBRID_DOCKER_BUILD_MODE must be one of: auto, always, never." >&2
      return 2
      ;;
  esac
}

record_docker_build_fingerprint() {
  docker_build_fingerprint > "$DOCKER_BUILD_FINGERPRINT_FILE"
}

docker_build_images_available() {
  local service_name
  local image_name
  for service_name in "${DOCKER_BUILD_SERVICES[@]}"; do
    image_name="${COMPOSE_PROJECT_NAME}-${service_name}:latest"
    if ! docker image inspect "$image_name" >/dev/null 2>&1; then
      return 1
    fi
  done
  return 0
}

local_python_install_fingerprint() {
  fingerprint_paths pyproject.toml
}

local_python_install_required() {
  case "$HYBRID_LOCAL_PYTHON_INSTALL_MODE" in
    always)
      return 0
      ;;
    never)
      return 1
      ;;
    auto)
      local current_fingerprint
      current_fingerprint="$(local_python_install_fingerprint)"
      [[ ! -f "$LOCAL_PYTHON_INSTALL_FINGERPRINT_FILE" ]] && return 0
      [[ "$(cat "$LOCAL_PYTHON_INSTALL_FINGERPRINT_FILE")" != "$current_fingerprint" ]]
      ;;
    *)
      echo "HYBRID_LOCAL_PYTHON_INSTALL_MODE must be one of: auto, always, never." >&2
      return 2
      ;;
  esac
}

record_local_python_install_fingerprint() {
  local_python_install_fingerprint > "$LOCAL_PYTHON_INSTALL_FINGERPRINT_FILE"
}

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
  for service_name in "${DOCKER_READY_SERVICES[@]}"; do
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

stop_existing_app_services_before_migration() {
  echo "Stopping app services before migrations..."
  docker_compose stop "${DOCKER_STOP_BEFORE_MIGRATION_SERVICES[@]}"
}

wait_for_docker_services_ready() {
  local timeout_seconds="$1"
  local poll_seconds="$2"
  shift 2
  local services=("$@")
  if (( ${#services[@]} == 0 )); then
    services=("${DOCKER_READY_SERVICES[@]}")
  fi
  local start_ts
  start_ts="$(date +%s)"

  while true; do
    local all_ready="true"
    local service_name
    for service_name in "${services[@]}"; do
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
        echo "Service '$service_name' container is not running (state=${state_status})."
        return 1
      fi

      if [[ "$service_name" == "postgres" || "$service_name" == "clickhouse" || "$service_name" == "api" ]]; then
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

    local now_ts
    now_ts="$(date +%s)"
    if (( now_ts - start_ts >= timeout_seconds )); then
      echo "Timed out waiting for Docker services readiness (${timeout_seconds}s)."
      return 1
    fi
    sleep "$poll_seconds"
  done
}

local_database_ready() {
  "${VENV_DIR}/bin/python" - <<'PY'
import os
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

database_url = os.environ["ORCHESTRATOR_DATABASE_URL"]
engine = create_engine(database_url, pool_pre_ping=True)
try:
    with engine.connect() as connection:
        connection.execute(text("select 1"))
except SQLAlchemyError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(1)
PY
}

wait_for_local_database_ready() {
  local timeout_seconds="$1"
  local poll_seconds="$2"
  local start_ts
  local now_ts
  local last_error_file
  last_error_file="$(mktemp)"
  start_ts="$(date +%s)"

  echo "Waiting for local database to accept SQL connections..."
  while true; do
    if local_database_ready 2>"$last_error_file"; then
      rm -f "$last_error_file"
      echo "Local database ready."
      return 0
    fi

    now_ts="$(date +%s)"
    if (( now_ts - start_ts >= timeout_seconds )); then
      echo "Timed out waiting for local database readiness (${timeout_seconds}s)."
      cat "$last_error_file" >&2
      rm -f "$last_error_file"
      return 1
    fi
    sleep "$poll_seconds"
  done
}

run_compose_up() {
  local output_file
  local compose_build_args=()
  output_file="$(mktemp)"

  if docker_build_required; then
    echo "Docker build inputs changed; building Compose service images."
    if ! docker_compose build "${DOCKER_BUILD_SERVICES[@]}"; then
      rm -f "$output_file"
      return 1
    fi
    record_docker_build_fingerprint
    compose_build_args=(--no-build)
  else
    local build_required_exit_code="$?"
    if (( build_required_exit_code > 1 )); then
      rm -f "$output_file"
      return "$build_required_exit_code"
    fi
    compose_build_args=(--no-build)
    echo "Docker build inputs unchanged; starting Compose services without rebuilding images."
  fi

  echo "Starting Docker infrastructure services..."
  if ! docker_compose up "${compose_build_args[@]}" -d --remove-orphans "${DOCKER_BASE_SERVICES[@]}"; then
    rm -f "$output_file"
    return 1
  fi

  echo "Waiting for Docker infrastructure to become healthy/ready..."
  wait_for_docker_services_ready "$DOCKER_WAIT_TIMEOUT_SECONDS" "$DOCKER_WAIT_INTERVAL_SECONDS" "${DOCKER_BASE_SERVICES[@]}"

  echo "Running database migrations..."
  if ! docker_compose run --rm --no-deps migrate; then
    rm -f "$output_file"
    return 1
  fi

  echo "Starting Docker runtime services..."
  if docker_compose up "${compose_build_args[@]}" --no-deps -d --remove-orphans "${DOCKER_RUNTIME_SERVICES[@]}" 2>&1 | tee "$output_file"; then
    rm -f "$output_file"
    echo "Waiting for Docker runtime services to become healthy/ready..."
    wait_for_docker_services_ready "$DOCKER_WAIT_TIMEOUT_SECONDS" "$DOCKER_WAIT_INTERVAL_SECONDS" "${DOCKER_BASE_SERVICES[@]}" "${DOCKER_RUNTIME_SERVICES[@]}"
    echo "Starting Docker post-API services..."
    if ! docker_compose up "${compose_build_args[@]}" --no-deps -d --remove-orphans "${DOCKER_POST_API_SERVICES[@]}"; then
      return 1
    fi
    wait_for_docker_services_ready "$DOCKER_WAIT_TIMEOUT_SECONDS" "$DOCKER_WAIT_INTERVAL_SECONDS" "${DOCKER_POST_API_SERVICES[@]}"
    return 0
  fi

  local compose_exit_code="${PIPESTATUS[0]}"
  local compose_output
  compose_output="$(cat "$output_file")"
  rm -f "$output_file"

  if printf '%s' "$compose_output" | grep -q 'No such container:' && all_target_services_have_live_container; then
    echo "Compose reported stale container metadata; continuing with readiness checks."
    wait_for_docker_services_ready "$DOCKER_WAIT_TIMEOUT_SECONDS" "$DOCKER_WAIT_INTERVAL_SECONDS"
    return $?
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
restart_existing_local_worker_if_owned
stop_existing_app_services_before_migration

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

if [[ -z "${ORCHESTRATOR_DATABASE_URL:-}" || "${ORCHESTRATOR_DATABASE_URL}" == *"localhost:4402"* || "${ORCHESTRATOR_DATABASE_URL}" == *"127.0.0.1:4402"* ]]; then
  export ORCHESTRATOR_DATABASE_URL="$LOCAL_DATABASE_URL"
fi
if [[ -z "${POSTGRES_URL:-}" || "${POSTGRES_URL}" == *"localhost:4402"* || "${POSTGRES_URL}" == *"127.0.0.1:4402"* ]]; then
  export POSTGRES_URL="$ORCHESTRATOR_DATABASE_URL"
fi
if [[ -z "${ORCHESTRATOR_PUBLIC_API_BASE_URL:-}" || "${ORCHESTRATOR_PUBLIC_API_BASE_URL}" == "http://localhost:4000" ]]; then
  export ORCHESTRATOR_PUBLIC_API_BASE_URL="$LOCAL_PUBLIC_API_BASE_URL"
fi
if [[ -z "${ORCHESTRATOR_ADMIN_UI_BASE_URL:-}" || "${ORCHESTRATOR_ADMIN_UI_BASE_URL}" == "http://localhost:4100" || "${ORCHESTRATOR_ADMIN_UI_BASE_URL}" == "http://127.0.0.1:4100" ]]; then
  export ORCHESTRATOR_ADMIN_UI_BASE_URL="$LOCAL_ADMIN_UI_BASE_URL"
fi
if [[ -z "${ORCHESTRATOR_CORS_ORIGINS:-}" || "${ORCHESTRATOR_CORS_ORIGINS}" == *"localhost:4100"* || "${ORCHESTRATOR_CORS_ORIGINS}" == *"127.0.0.1:4100"* ]]; then
  export ORCHESTRATOR_CORS_ORIGINS="http://localhost:${ADMIN_UI_PORT},http://127.0.0.1:${ADMIN_UI_PORT},https://master-builder.vercel.app"
fi
if [[ -z "${NEXT_PUBLIC_API_BASE_URL:-}" || "${NEXT_PUBLIC_API_BASE_URL}" == "http://localhost:4000" ]]; then
  export NEXT_PUBLIC_API_BASE_URL="$LOCAL_PUBLIC_API_BASE_URL"
fi
export ORCHESTRATOR_CLICKHOUSE_HTTP_URL="${ORCHESTRATOR_CLICKHOUSE_HTTP_URL:-http://127.0.0.1:${MASTER_BUILDER_CLICKHOUSE_HTTP_PORT}}"
export ORCHESTRATOR_CLICKHOUSE_DATABASE="${ORCHESTRATOR_CLICKHOUSE_DATABASE:-master_builder}"
export ORCHESTRATOR_CLICKHOUSE_USERNAME="${ORCHESTRATOR_CLICKHOUSE_USERNAME:-master_builder}"
export ORCHESTRATOR_CLICKHOUSE_PASSWORD="${ORCHESTRATOR_CLICKHOUSE_PASSWORD:-master_builder}"
export ORCHESTRATOR_OTEL_ENABLED="${ORCHESTRATOR_OTEL_ENABLED:-true}"
export ORCHESTRATOR_OTEL_SERVICE_NAMESPACE="${ORCHESTRATOR_OTEL_SERVICE_NAMESPACE:-master-builder}"
export ORCHESTRATOR_OTEL_EXPORTER_OTLP_ENDPOINT="${ORCHESTRATOR_OTEL_EXPORTER_OTLP_ENDPOINT:-http://127.0.0.1:${MASTER_BUILDER_OTEL_HTTP_PORT}}"
export ORCHESTRATOR_OTEL_TRACES_SAMPLE_RATIO="${ORCHESTRATOR_OTEL_TRACES_SAMPLE_RATIO:-1.0}"
export ORCHESTRATOR_WORKER_CAPABILITIES="${ORCHESTRATOR_WORKER_CAPABILITIES:-macos}"
export ORCHESTRATOR_AGENT_ID="${ORCHESTRATOR_AGENT_ID:-worker-macos-local}"
export ORCHESTRATOR_CODEX_SANDBOX_MODE="${ORCHESTRATOR_CODEX_SANDBOX_MODE:-danger-full-access}"
export REMOVED_PRIVATE_CREDENTIAL"${ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR:-${WORKER_CHECKOUT_DIR}}"

if local_python_install_required; then
  echo "Installing/updating Python dependencies in ${VENV_DIR}..."
  "${VENV_DIR}/bin/python" -m pip install --upgrade pip
  "${VENV_DIR}/bin/pip" install -e .
  record_local_python_install_fingerprint
else
  echo "Local Python dependencies are current; skipping editable install."
fi

wait_for_local_database_ready "$DOCKER_WAIT_TIMEOUT_SECONDS" "$DOCKER_WAIT_INTERVAL_SECONDS"

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
"${VENV_DIR}/bin/python" -m orchestrator worker-runs
