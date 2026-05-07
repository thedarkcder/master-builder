#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/your-org/master-builder.git}"
REPO_REF="${REPO_REF:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/master-builder}"
DEPLOY_DIR="${INSTALL_DIR}/src"
ENV_FILE="${INSTALL_DIR}/.env"
COMPOSE_FILE="${DEPLOY_DIR}/deploy/hetzner/docker-compose.prod.yml"
GPU_COMPOSE_FILE="${DEPLOY_DIR}/deploy/hetzner/docker-compose.llm-gpu.yml"
API_PORT="${MASTER_BUILDER_API_PORT:-4000}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:${API_PORT}/health}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "bootstrap.sh must run as root" >&2
  exit 1
fi

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

require_env() {
  local key="$1"
  if [[ -z "${!key:-}" ]]; then
    echo "missing required environment variable: ${key}" >&2
    exit 1
  fi
}

require_secret_crypto_config() {
  require_env ORCHESTRATOR_SECRET_CRYPTO_PROVIDER
  case "${ORCHESTRATOR_SECRET_CRYPTO_PROVIDER}" in
    vault_transit)
      require_env ORCHESTRATOR_VAULT_ADDR
      require_env ORCHESTRATOR_VAULT_TOKEN
      require_env ORCHESTRATOR_VAULT_TRANSIT_KEY
      ;;
    aws_kms)
      require_env ORCHESTRATOR_AWS_KMS_REGION
      require_env ORCHESTRATOR_AWS_KMS_KEY_ID
      ;;
    gcp_kms)
      require_env ORCHESTRATOR_GCP_KMS_KEY_NAME
      ;;
    fernet_legacy)
      require_env ORCHESTRATOR_SECRETS_ENCRYPTION_KEY
      ;;
    *)
      echo "unsupported ORCHESTRATOR_SECRET_CRYPTO_PROVIDER: ${ORCHESTRATOR_SECRET_CRYPTO_PROVIDER}" >&2
      exit 1
      ;;
  esac
}

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y ca-certificates curl git jq gnupg lsb-release
}

install_docker() {
  if need_cmd docker && need_cmd docker-compose; then
    return
  fi

  install -m 0755 -d /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
  fi

  local codename
  codename="$(. /etc/os-release && echo "${VERSION_CODENAME}")"
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable" \
    >/etc/apt/sources.list.d/docker.list

  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable docker
  systemctl start docker
}

checkout_repo() {
  mkdir -p "${INSTALL_DIR}"
  if [[ -d "${DEPLOY_DIR}/.git" ]]; then
    git -C "${DEPLOY_DIR}" fetch --all --tags
    git -C "${DEPLOY_DIR}" checkout "${REPO_REF}"
    git -C "${DEPLOY_DIR}" pull --ff-only
  else
    git clone "${REPO_URL}" "${DEPLOY_DIR}"
    git -C "${DEPLOY_DIR}" checkout "${REPO_REF}"
  fi
}

write_env_file() {
  require_env ORCHESTRATOR_ADMIN_PASSWORD
  require_env ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET
  require_env ORCHESTRATOR_ATLASSIAN_OAUTH_STATE_SECRET
  require_secret_crypto_config
  require_env ORCHESTRATOR_CLICKHOUSE_PASSWORD
  require_env NEXTAUTH_SECRET
  require_env ORCHESTRATOR_EMAIL_FROM_ADDRESS

  cat >"${ENV_FILE}" <<EOF
POSTGRES_DB=${POSTGRES_DB:-orchestrator}
POSTGRES_USER=${POSTGRES_USER:-orchestrator}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-orchestrator}

MASTER_BUILDER_API_PORT=${MASTER_BUILDER_API_PORT:-4000}
MASTER_BUILDER_ADMIN_UI_PORT=${MASTER_BUILDER_ADMIN_UI_PORT:-4100}

ORCHESTRATOR_ADMIN_USERNAME=${ORCHESTRATOR_ADMIN_USERNAME:-admin}
ORCHESTRATOR_ADMIN_PASSWORD=${ORCHESTRATOR_ADMIN_PASSWORD}
ORCHESTRATOR_PUBLIC_API_BASE_URL=${ORCHESTRATOR_PUBLIC_API_BASE_URL:-http://localhost:4000}
ORCHESTRATOR_ADMIN_UI_BASE_URL=${ORCHESTRATOR_ADMIN_UI_BASE_URL:-http://localhost:4100}
ORCHESTRATOR_CORS_ORIGINS=${ORCHESTRATOR_CORS_ORIGINS:-http://localhost:4100,http://127.0.0.1:4100}
ORCHESTRATOR_CORS_ORIGIN_REGEX=${ORCHESTRATOR_CORS_ORIGIN_REGEX:-}

ORCHESTRATOR_DATABASE_URL=${ORCHESTRATOR_DATABASE_URL:-postgresql+psycopg://orchestrator:orchestrator@postgres:5432/orchestrator}
POSTGRES_URL=${POSTGRES_URL:-postgresql+psycopg://orchestrator:orchestrator@postgres:5432/orchestrator}
ORCHESTRATOR_CLICKHOUSE_HTTP_URL=${ORCHESTRATOR_CLICKHOUSE_HTTP_URL:-http://clickhouse:8123}
ORCHESTRATOR_CLICKHOUSE_DATABASE=${ORCHESTRATOR_CLICKHOUSE_DATABASE:-master_builder}
ORCHESTRATOR_CLICKHOUSE_USERNAME=${ORCHESTRATOR_CLICKHOUSE_USERNAME:-master_builder}
ORCHESTRATOR_CLICKHOUSE_PASSWORD=${ORCHESTRATOR_CLICKHOUSE_PASSWORD}

ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET=${ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET}
ORCHESTRATOR_ATLASSIAN_OAUTH_STATE_SECRET=${ORCHESTRATOR_ATLASSIAN_OAUTH_STATE_SECRET}
ORCHESTRATOR_SECRET_CRYPTO_PROVIDER=${ORCHESTRATOR_SECRET_CRYPTO_PROVIDER}
ORCHESTRATOR_VAULT_ADDR=${ORCHESTRATOR_VAULT_ADDR:-}
ORCHESTRATOR_VAULT_TOKEN=${ORCHESTRATOR_VAULT_TOKEN:-}
ORCHESTRATOR_VAULT_NAMESPACE=${ORCHESTRATOR_VAULT_NAMESPACE:-}
ORCHESTRATOR_VAULT_TRANSIT_KEY=${ORCHESTRATOR_VAULT_TRANSIT_KEY:-}
ORCHESTRATOR_AWS_KMS_REGION=${ORCHESTRATOR_AWS_KMS_REGION:-}
ORCHESTRATOR_AWS_KMS_KEY_ID=${ORCHESTRATOR_AWS_KMS_KEY_ID:-}
ORCHESTRATOR_GCP_KMS_KEY_NAME=${ORCHESTRATOR_GCP_KMS_KEY_NAME:-}
ORCHESTRATOR_SECRETS_ENCRYPTION_KEY=${ORCHESTRATOR_SECRETS_ENCRYPTION_KEY:-}

ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER=${ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER:-resend}
ORCHESTRATOR_EMAIL_FROM_NAME=${ORCHESTRATOR_EMAIL_FROM_NAME:-Master Builder}
ORCHESTRATOR_EMAIL_FROM_ADDRESS=${ORCHESTRATOR_EMAIL_FROM_ADDRESS}
ORCHESTRATOR_RESEND_API_KEY=${ORCHESTRATOR_RESEND_API_KEY:-}
ORCHESTRATOR_SMTP_HOST=${ORCHESTRATOR_SMTP_HOST:-}
ORCHESTRATOR_SMTP_PORT=${ORCHESTRATOR_SMTP_PORT:-}
ORCHESTRATOR_SMTP_USERNAME=${ORCHESTRATOR_SMTP_USERNAME:-}
ORCHESTRATOR_SMTP_PASSWORD=${ORCHESTRATOR_SMTP_PASSWORD:-}
ORCHESTRATOR_SMTP_USE_TLS=${ORCHESTRATOR_SMTP_USE_TLS:-false}
ORCHESTRATOR_SMTP_USE_SSL=${ORCHESTRATOR_SMTP_USE_SSL:-false}


ORCHESTRATOR_VOICE_STT_PROVIDER=${ORCHESTRATOR_VOICE_STT_PROVIDER:-whisper}
ORCHESTRATOR_VOICE_TTS_PROVIDER=${ORCHESTRATOR_VOICE_TTS_PROVIDER:-pocket_tts}
HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
LLAMA_CPP_MODEL_URL=${LLAMA_CPP_MODEL_URL:-}
LLAMA_CPP_MODEL_PATH=${LLAMA_CPP_MODEL_PATH:-/models/gemma-4.gguf}
LLAMA_CPP_PORT=${LLAMA_CPP_PORT:-8080}
ENABLE_LLAMA_GPU=${ENABLE_LLAMA_GPU:-true}
LLAMA_CPP_N_GPU_LAYERS=${LLAMA_CPP_N_GPU_LAYERS:-999}
LLAMA_CPP_CTX_SIZE=${LLAMA_CPP_CTX_SIZE:-8192}

NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL:-http://localhost:4000}
NEXTAUTH_URL=${NEXTAUTH_URL:-http://localhost:4100}
NEXTAUTH_SECRET=${NEXTAUTH_SECRET}
EOF
}

deploy_stack() {
  local compose_args=(
    --env-file "${ENV_FILE}"
    -f "${COMPOSE_FILE}"
  )

  if [[ "${ENABLE_LLAMA_GPU:-true}" == "true" ]]; then
    compose_args+=(-f "${GPU_COMPOSE_FILE}")
  fi

  docker compose "${compose_args[@]}" up --build --force-recreate --exit-code-from migrate migrate
  docker compose "${compose_args[@]}" up -d --build
}

wait_for_health() {
  local max_attempts=60
  local attempt=1

  while [[ "${attempt}" -le "${max_attempts}" ]]; do
    if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
      echo "master-builder API is healthy at ${HEALTH_URL}"
      return 0
    fi
    sleep 5
    attempt=$((attempt + 1))
  done

  echo "health check did not pass after $((max_attempts * 5)) seconds: ${HEALTH_URL}" >&2
  return 1
}

main() {
  install_packages
  install_docker
  checkout_repo
  write_env_file
  deploy_stack
  wait_for_health
  cat <<EOF
Deployment complete.
API:      ${ORCHESTRATOR_PUBLIC_API_BASE_URL:-http://localhost:4000}
Admin UI: ${ORCHESTRATOR_ADMIN_UI_BASE_URL:-http://localhost:4100}
EOF
}

main "$@"
