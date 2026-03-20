#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export ORCHESTRATOR_LIVE_VOICE_TRANSPORT_COMMAND="${ORCHESTRATOR_LIVE_VOICE_TRANSPORT_COMMAND:-/usr/local/bin/live-voice-transport}"
export ORCHESTRATOR_LIVE_VOICE_TRANSPORT_STARTUP_TIMEOUT_SECONDS="${ORCHESTRATOR_LIVE_VOICE_TRANSPORT_STARTUP_TIMEOUT_SECONDS:-10}"
export ORCHESTRATOR_LIVE_VOICE_TRANSPORT_REQUEST_TIMEOUT_SECONDS="${ORCHESTRATOR_LIVE_VOICE_TRANSPORT_REQUEST_TIMEOUT_SECONDS:-10}"

DOCKER_SERVICES=(
  postgres
  api
  worker
  knowledge-sync
  discord-gateway
  discord-live-voice
  tailscale
)

service_container_name() {
  printf 'master-builder-%s' "$1"
}

service_is_ready() {
  local service_name="$1"
  local container_name
  local state_summary
  container_name="$(service_container_name "$service_name")"
  state_summary="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$container_name" 2>/dev/null || true)"
  if [[ -z "$state_summary" ]]; then
    return 1
  fi

  case "$service_name" in
    postgres|api)
      [[ "$state_summary" == *"running healthy"* ]]
      ;;
    *)
      [[ "$state_summary" == running* ]]
      ;;
  esac
}

docker_services_ready() {
  local service_name
  for service_name in "${DOCKER_SERVICES[@]}"; do
    if ! service_is_ready "$service_name"; then
      return 1
    fi
  done
  return 0
}

compose_output=""
if ! compose_output="$(docker compose up --build -d --remove-orphans "${DOCKER_SERVICES[@]}" 2>&1)"; then
  printf '%s\n' "$compose_output"
  if printf '%s' "$compose_output" | grep -q 'No such container:' && docker_services_ready; then
    echo "Docker services are ready; continuing despite stale Compose container metadata."
  else
    exit 1
  fi
else
  printf '%s\n' "$compose_output"
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to start admin-ui."
  exit 1
fi

if [[ ! -f "${ROOT_DIR}/admin-ui/package.json" ]]; then
  echo "admin-ui/package.json not found."
  exit 1
fi

cd "${ROOT_DIR}/admin-ui"
exec npm run dev
