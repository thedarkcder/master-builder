#!/usr/bin/env bash
set -euo pipefail

if ! command -v hcloud >/dev/null 2>&1; then
  echo "missing required command: hcloud" >&2
  exit 1
fi

prompt_default() {
  local var_name="$1"
  local label="$2"
  local default_value="${3:-}"
  local current="${!var_name:-}"
  local prompt_value

  if [[ -n "${current}" ]]; then
    return
  fi

  if [[ -n "${default_value}" ]]; then
    read -r -p "${label} [${default_value}]: " prompt_value
    if [[ -z "${prompt_value}" ]]; then
      prompt_value="${default_value}"
    fi
  else
    read -r -p "${label}: " prompt_value
  fi
  export "${var_name}=${prompt_value}"
}

prompt_secret() {
  local var_name="$1"
  local label="$2"
  local current="${!var_name:-}"
  local prompt_value

  if [[ -n "${current}" ]]; then
    return
  fi

  read -r -s -p "${label}: " prompt_value
  echo
  export "${var_name}=${prompt_value}"
}

require_non_empty() {
  local var_name="$1"
  if [[ -z "${!var_name:-}" ]]; then
    echo "missing required value: ${var_name}" >&2
    exit 1
  fi
}

SERVER_NAME="${SERVER_NAME:-master-builder-01}"
SERVER_TYPE="${SERVER_TYPE:-cpx31}"
SERVER_IMAGE="${SERVER_IMAGE:-ubuntu-24.04}"
SERVER_LOCATION="${SERVER_LOCATION:-nbg1}"
REPO_URL="${REPO_URL:-https://github.com/your-org/master-builder.git}"
REPO_REF="${REPO_REF:-main}"

export HCLOUD_TOKEN

collect_inputs() {
  prompt_secret HCLOUD_TOKEN "HCLOUD_TOKEN"
  prompt_default HCLOUD_SSH_KEY_NAME "HCLOUD_SSH_KEY_NAME (existing key in Hetzner)" ""
  prompt_default SERVER_NAME "SERVER_NAME" "${SERVER_NAME}"
  prompt_default SERVER_TYPE "SERVER_TYPE" "${SERVER_TYPE}"
  prompt_default SERVER_LOCATION "SERVER_LOCATION" "${SERVER_LOCATION}"
  prompt_default SERVER_IMAGE "SERVER_IMAGE" "${SERVER_IMAGE}"
  prompt_default REPO_URL "REPO_URL" "${REPO_URL}"
  prompt_default REPO_REF "REPO_REF" "${REPO_REF}"
  prompt_default LLAMA_CPP_MODEL_URL "LLAMA_CPP_MODEL_URL (direct GGUF URL)" "${LLAMA_CPP_MODEL_URL:-}"
  prompt_default LLAMA_CPP_MODEL_PATH "LLAMA_CPP_MODEL_PATH" "${LLAMA_CPP_MODEL_PATH:-/models/gemma-4.gguf}"
  prompt_default LLAMA_CPP_PORT "LLAMA_CPP_PORT" "${LLAMA_CPP_PORT:-8080}"
  prompt_default ENABLE_LLAMA_GPU "ENABLE_LLAMA_GPU (true/false)" "${ENABLE_LLAMA_GPU:-true}"
  prompt_default LLAMA_CPP_N_GPU_LAYERS "LLAMA_CPP_N_GPU_LAYERS" "${LLAMA_CPP_N_GPU_LAYERS:-999}"
  prompt_default LLAMA_CPP_CTX_SIZE "LLAMA_CPP_CTX_SIZE" "${LLAMA_CPP_CTX_SIZE:-8192}"

  prompt_secret ORCHESTRATOR_ADMIN_PASSWORD "ORCHESTRATOR_ADMIN_PASSWORD"
  prompt_secret ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET "ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET"
  prompt_secret ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET "ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET"
  prompt_secret ORCHESTRATOR_SECRETS_ENCRYPTION_KEY "ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"
  prompt_secret NEXTAUTH_SECRET "NEXTAUTH_SECRET"

  prompt_default ORCHESTRATOR_EMAIL_FROM_ADDRESS "ORCHESTRATOR_EMAIL_FROM_ADDRESS" "no-reply@example.com"
  prompt_default ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER "ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER" "${ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER:-resend}"

  if [[ "${ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER}" == "resend" ]]; then
    prompt_secret ORCHESTRATOR_RESEND_API_KEY "ORCHESTRATOR_RESEND_API_KEY"
  else
    prompt_default ORCHESTRATOR_RESEND_API_KEY "ORCHESTRATOR_RESEND_API_KEY" "${ORCHESTRATOR_RESEND_API_KEY:-}"
  fi

  require_non_empty HCLOUD_TOKEN
  require_non_empty HCLOUD_SSH_KEY_NAME
  require_non_empty ORCHESTRATOR_ADMIN_PASSWORD
  require_non_empty ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET
  require_non_empty ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET
  require_non_empty ORCHESTRATOR_SECRETS_ENCRYPTION_KEY
  require_non_empty NEXTAUTH_SECRET
  require_non_empty ORCHESTRATOR_EMAIL_FROM_ADDRESS
  require_non_empty LLAMA_CPP_MODEL_PATH
  require_non_empty ENABLE_LLAMA_GPU
  require_non_empty LLAMA_CPP_N_GPU_LAYERS
}

tmp_user_data="$(mktemp)"
cleanup() {
  rm -f "${tmp_user_data}"
}
trap cleanup EXIT

collect_inputs

cat >"${tmp_user_data}" <<EOF
#cloud-config
package_update: true
runcmd:
  - export REPO_URL='${REPO_URL}'
  - export REPO_REF='${REPO_REF}'
  - export LLAMA_CPP_MODEL_URL='${LLAMA_CPP_MODEL_URL}'
  - export LLAMA_CPP_MODEL_PATH='${LLAMA_CPP_MODEL_PATH}'
  - export LLAMA_CPP_PORT='${LLAMA_CPP_PORT}'
  - export ENABLE_LLAMA_GPU='${ENABLE_LLAMA_GPU}'
  - export LLAMA_CPP_N_GPU_LAYERS='${LLAMA_CPP_N_GPU_LAYERS}'
  - export LLAMA_CPP_CTX_SIZE='${LLAMA_CPP_CTX_SIZE}'
  - export ORCHESTRATOR_ADMIN_PASSWORD='${ORCHESTRATOR_ADMIN_PASSWORD}'
  - export ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET='${ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET}'
  - export ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET='${ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET}'
  - export ORCHESTRATOR_SECRETS_ENCRYPTION_KEY='${ORCHESTRATOR_SECRETS_ENCRYPTION_KEY}'
  - export NEXTAUTH_SECRET='${NEXTAUTH_SECRET}'
  - export ORCHESTRATOR_EMAIL_FROM_ADDRESS='${ORCHESTRATOR_EMAIL_FROM_ADDRESS}'
  - export ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER='${ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER:-resend}'
  - export ORCHESTRATOR_RESEND_API_KEY='${ORCHESTRATOR_RESEND_API_KEY:-}'
  - git clone '${REPO_URL}' /opt/master-builder-bootstrap
  - bash /opt/master-builder-bootstrap/deploy/hetzner/bootstrap.sh
EOF

hcloud server create \
  --name "${SERVER_NAME}" \
  --type "${SERVER_TYPE}" \
  --image "${SERVER_IMAGE}" \
  --location "${SERVER_LOCATION}" \
  --ssh-key "${HCLOUD_SSH_KEY_NAME}" \
  --user-data-from-file "${tmp_user_data}"

echo "Server creation requested: ${SERVER_NAME}"
echo "Use 'hcloud server list' and 'hcloud server describe ${SERVER_NAME}' to monitor provisioning."
