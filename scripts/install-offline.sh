#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# ARK Offline Installer (Run on Air-Gapped Linux GPU Host)
# Verifies checksums, checks NVIDIA GPU & disk space, loads Docker images,
# places/unpacks model weights into ./artifacts/models/, and starts ARK.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ARK_VERSION="$(tr -d '[:space:]' < "${REPO_ROOT}/VERSION" 2>/dev/null || echo '0.0.0-dev')"

ARTIFACTS_DIR="${REPO_ROOT}/artifacts"
IMAGES_ARCHIVE=""
MODELS_ARCHIVE=""
MODELS_SRC=""
MIN_FREE_GB=100
DOMAIN_ARG=""
BACKUP_DIR_ARG=""
NON_INTERACTIVE=false
SKIP_GPU_CHECK=false
NO_START=false

log()  { printf '[%s] [INFO]  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
warn() { printf '[%s] [WARN]  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
err()  { printf '[%s] [ERROR] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Installs and starts ARK on an air-gapped Linux GPU server.

Options:
  -a, --artifacts-dir PATH   Path to artifacts directory (default: ${REPO_ROOT}/artifacts)
  -i, --images-archive PATH  Path to ark-images.tar.zst (default: <artifacts-dir>/ark-images.tar.zst)
  -M, --models-archive PATH  Path to ark-models.tar.zst if models were archived
  -s, --models-src PATH      Optional external directory containing model folders to copy into ./artifacts/models/
  -m, --min-free-gb N        Minimum free disk space required in GB (default: ${MIN_FREE_GB})
  -d, --domain FQDN          Set ARK_DOMAIN & WEBUI_URL (e.g. ark.corp.internal)
  -b, --backup-dir PATH      Set initial ARK_BACKUP_DIR (e.g. /mnt/nas/ark-backups)
      --non-interactive      Do not prompt for domain or backup directory
      --skip-gpu-check       Skip nvidia-smi GPU verification (for testing only)
      --no-start             Load images and prepare volumes without running docker compose up -d
  -V, --version              Print ARK version and exit
  -h, --help                 Show this help message and exit
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -a|--artifacts-dir)  ARTIFACTS_DIR="$2"; shift 2 ;;
    -i|--images-archive) IMAGES_ARCHIVE="$2"; shift 2 ;;
    -M|--models-archive) MODELS_ARCHIVE="$2"; shift 2 ;;
    -s|--models-src)     MODELS_SRC="$2"; shift 2 ;;
    -m|--min-free-gb)    MIN_FREE_GB="$2"; shift 2 ;;
    -d|--domain)         DOMAIN_ARG="$2"; shift 2 ;;
    -b|--backup-dir)     BACKUP_DIR_ARG="$2"; shift 2 ;;
    --non-interactive)   NON_INTERACTIVE=true; shift ;;
    --skip-gpu-check)    SKIP_GPU_CHECK=true; shift ;;
    --no-start)          NO_START=true; shift ;;
    -V|--version)        printf 'ARK v%s\n' "${ARK_VERSION}"; exit 0 ;;
    -h|--help)           usage; exit 0 ;;
    *)                   err "Unknown argument: $1" ;;
  esac
done

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || err "Required command not found: $1"
}

verify_sha256() {
  local file_path="$1"
  local sha_file="${file_path}.sha256"
  if [[ ! -f "${sha_file}" ]]; then
    err "Checksum file missing: ${sha_file}. Refusing to load unverified artifact."
  fi
  log "Verifying SHA-256 checksum for ${file_path}..."
  local dir_name base_name
  dir_name="$(dirname "${file_path}")"
  base_name="$(basename "${file_path}")"
  (cd "${dir_name}" && sha256sum -c "$(basename "${sha_file}")" --status) || \
    err "SHA-256 checksum verification FAILED for ${base_name}!"
  log "Checksum OK: ${base_name}"
}

check_disk_space() {
  local target_dir="$1"
  local min_gb="$2"
  local avail_kb
  avail_kb=$(df -Pk "${target_dir}" | awk 'NR==2 {print $4}')
  local avail_gb=$(( avail_kb / 1024 / 1024 ))
  log "Disk space check on ${target_dir}: ${avail_gb} GB available (minimum ${min_gb} GB)."
  if (( avail_gb < min_gb )); then
    err "Insufficient disk space: ${avail_gb} GB < ${min_gb} GB required."
  fi
}

check_gpu() {
  if [[ "${SKIP_GPU_CHECK}" == true ]]; then
    warn "Skipping NVIDIA GPU check (--skip-gpu-check)."
    return
  fi
  require_cmd nvidia-smi
  log "Verifying NVIDIA GPU availability..."
  if ! nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader; then
    err "nvidia-smi failed. Ensure NVIDIA drivers and nvidia-container-toolkit are installed."
  fi
}

main() {
  log "Starting ARK Offline Installer (ARK v${ARK_VERSION})..."
  require_cmd docker
  require_cmd zstd
  require_cmd sha256sum
  require_cmd df

  docker compose version >/dev/null 2>&1 || err "Docker Compose v2 plugin ('docker compose') is required."

  if [[ -f "${ARTIFACTS_DIR}/ark-bundle.json" ]]; then
    if [[ -f "${ARTIFACTS_DIR}/ark-bundle.json.sha256" ]]; then
      verify_sha256 "${ARTIFACTS_DIR}/ark-bundle.json"
    fi
    local bundle_ver
    bundle_ver=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("ark_version","unknown"))' "${ARTIFACTS_DIR}/ark-bundle.json" 2>/dev/null || echo "unknown")
    log "Detected bundle metadata: ARK v${bundle_ver}"
    if [[ "${bundle_ver}" != "unknown" && "${bundle_ver}" != "${ARK_VERSION}" ]]; then
      warn "Bundle version (${bundle_ver}) differs from repository VERSION (${ARK_VERSION})."
    fi
  fi

  check_gpu
  check_disk_space "${REPO_ROOT}" "${MIN_FREE_GB}"

  if [[ ! -f "${REPO_ROOT}/.env" ]]; then
    err "${REPO_ROOT}/.env not found. Copy .env.example to .env and configure site secrets before installing."
  fi

  set -a
  # shellcheck disable=SC1091
  source "${REPO_ROOT}/.env"
  set +a

  IMAGES_ARCHIVE="${IMAGES_ARCHIVE:-${ARTIFACTS_DIR}/ark-images.tar.zst}"
  MODELS_ARCHIVE="${MODELS_ARCHIVE:-${ARTIFACTS_DIR}/ark-models.tar.zst}"

  if [[ ! -f "${IMAGES_ARCHIVE}" ]]; then
    err "Images archive not found: ${IMAGES_ARCHIVE}"
  fi
  verify_sha256 "${IMAGES_ARCHIVE}"

  log "Decompressing and loading Docker images from ${IMAGES_ARCHIVE}..."
  zstd -d -c "${IMAGES_ARCHIVE}" | docker load
  log "Docker images loaded successfully."

  local target_models_dir="${REPO_ROOT}/artifacts/models"
  mkdir -p "${target_models_dir}" "${REPO_ROOT}/artifacts/snapshots"

  if [[ -n "${MODELS_SRC}" ]]; then
    [[ -d "${MODELS_SRC}" ]] || err "Specified --models-src directory does not exist: ${MODELS_SRC}"
    log "Copying model weights from ${MODELS_SRC} -> ${target_models_dir}..."
    cp -a "${MODELS_SRC}/." "${target_models_dir}/"
  elif [[ -f "${MODELS_ARCHIVE}" ]]; then
    verify_sha256 "${MODELS_ARCHIVE}"
    log "Unpacking model weights from ${MODELS_ARCHIVE} -> ${REPO_ROOT}/artifacts/..."
    zstd -d -c "${MODELS_ARCHIVE}" | tar -xf - -C "${REPO_ROOT}/artifacts"
  fi

  for model_dir in \
    "${LLM_MODEL_DIR:-Qwen--Qwen2.5-14B-Instruct}" \
    "${EMBED_MODEL_DIR:-BAAI--bge-m3}" \
    "${RERANK_MODEL_DIR:-BAAI--bge-reranker-v2-m3}"; do
    if [[ ! -d "${target_models_dir}/${model_dir}" ]]; then
      err "Expected model directory missing: ${target_models_dir}/${model_dir}"
    fi
    log "Verified local model weights: ${target_models_dir}/${model_dir}"
  done

  log "Preparing persistent data & backup directories..."
  local selected_domain="${DOMAIN_ARG:-${ARK_DOMAIN:-ark.local}}"
  local selected_backup="${BACKUP_DIR_ARG:-${ARK_BACKUP_DIR:-./data/backups}}"

  if [[ "${NON_INTERACTIVE}" == false && -t 0 ]]; then
    if [[ -z "${DOMAIN_ARG}" ]]; then
      read -r -p "Enter internal FQDN for ARK [${selected_domain}]: " input_domain
      selected_domain="${input_domain:-${selected_domain}}"
    fi
    if [[ -z "${BACKUP_DIR_ARG}" ]]; then
      read -r -p "Enter backup destination path [${selected_backup}]: " input_backup
      selected_backup="${input_backup:-${selected_backup}}"
    fi
  fi

  # Persist selected domain & backup directory to .env
  sed -i "s|^ARK_DOMAIN=.*|ARK_DOMAIN=${selected_domain}|" "${REPO_ROOT}/.env"
  sed -i "s|^WEBUI_URL=.*|WEBUI_URL=https://${selected_domain}|" "${REPO_ROOT}/.env"
  sed -i "s|^ARK_BACKUP_DIR=.*|ARK_BACKUP_DIR=${selected_backup}|" "${REPO_ROOT}/.env"

  # Update Keycloak realm redirect URIs & OIDC client secrets to match .env
  if [[ -f "${REPO_ROOT}/config/keycloak/ark-realm.json" ]]; then
    sed -i "s|https://ark\.local|https://${selected_domain}|g" "${REPO_ROOT}/config/keycloak/ark-realm.json"
    if [[ -n "${OIDC_OPENWEBUI_CLIENT_SECRET:-}" ]]; then
      sed -i "s|ark-openwebui-oidc-secret-change-me|${OIDC_OPENWEBUI_CLIENT_SECRET}|g" "${REPO_ROOT}/config/keycloak/ark-realm.json"
    fi
    if [[ -n "${OIDC_OAUTH2_PROXY_CLIENT_SECRET:-}" ]]; then
      sed -i "s|ark-oauth2-proxy-secret-change-me|${OIDC_OAUTH2_PROXY_CLIENT_SECRET}|g" "${REPO_ROOT}/config/keycloak/ark-realm.json"
    fi
  fi

  mkdir -p \
    "${REPO_ROOT}/data/qdrant" \
    "${REPO_ROOT}/data/postgres" \
    "${REPO_ROOT}/data/open-webui" \
    "${REPO_ROOT}/data/caddy/data" \
    "${REPO_ROOT}/data/caddy/config" \
    "${REPO_ROOT}/data/audit" \
    "${REPO_ROOT}/certs" \
    "${selected_backup}"

  if [[ "${NO_START}" == true ]]; then
    log "Offline installation prepared (--no-start). Run 'docker compose up -d' when ready."
    exit 0
  fi

  log "Starting ARK stack in air-gapped mode..."
  (cd "${REPO_ROOT}" && docker compose up -d)

  log "ARK stack started. Check service status with: docker compose ps"
}

main "$@"
