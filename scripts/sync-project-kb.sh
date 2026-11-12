#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# ARK Project Knowledge Base Snapshot Sync
# Imports a Qdrant .snapshot file exported from HQ into the offline office
# Qdrant instance into the specified project collection.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SNAPSHOT_FILE=""
COLLECTION_NAME=""
ENV_FILE="${REPO_ROOT}/.env"
SKIP_CHECKSUM=false
MIN_FREE_GB=10

log()  { printf '[%s] [INFO]  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
warn() { printf '[%s] [WARN]  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
err()  { printf '[%s] [ERROR] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: $(basename "$0") --snapshot PATH --collection NAME [OPTIONS]

Restores a Qdrant snapshot into a target collection in the offline ARK Qdrant instance.

Options:
  -f, --snapshot PATH      Path to the .snapshot file (required)
  -c, --collection NAME    Target Qdrant collection name (required, e.g. Project_Alpha)
  -e, --env-file PATH      Path to .env file (default: ${REPO_ROOT}/.env)
      --skip-checksum      Skip .sha256 verification if no checksum file is provided
  -h, --help               Show this help message and exit
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--snapshot)      [[ $# -ge 2 ]] || err "Missing value for $1"; SNAPSHOT_FILE="$2"; shift 2 ;;
    -c|--collection)    [[ $# -ge 2 ]] || err "Missing value for $1"; COLLECTION_NAME="$2"; shift 2 ;;
    -e|--env-file)      [[ $# -ge 2 ]] || err "Missing value for $1"; ENV_FILE="$2"; shift 2 ;;
    --skip-checksum)    SKIP_CHECKSUM=true; shift ;;
    -h|--help)          usage; exit 0 ;;
    *)                  err "Unknown argument: $1" ;;
  esac
done

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || err "Required command not found: $1"
}

main() {
  require_cmd curl
  require_cmd python3
  require_cmd sha256sum
  require_cmd df

  [[ -n "${SNAPSHOT_FILE}" ]] || { usage; err "--snapshot PATH is required."; }
  [[ -n "${COLLECTION_NAME}" ]] || { usage; err "--collection NAME is required."; }
  [[ -f "${SNAPSHOT_FILE}" ]] || err "Snapshot file not found: ${SNAPSHOT_FILE}"

  if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
  fi

  local qdrant_url="${QDRANT_URL:-http://127.0.0.1:6333}"
  qdrant_url="${qdrant_url%/}"
  local qdrant_api_key="${QDRANT_API_KEY:-}"

  local avail_kb
  avail_kb=$(df -Pk "${REPO_ROOT}" | awk 'NR==2 {print $4}')
  local avail_gb=$(( avail_kb / 1024 / 1024 ))
  if (( avail_gb < MIN_FREE_GB )); then
    err "Insufficient free disk space: ${avail_gb} GB < ${MIN_FREE_GB} GB required."
  fi

  local sha_file="${SNAPSHOT_FILE}.sha256"
  if [[ -f "${sha_file}" ]]; then
    log "Verifying SHA-256 checksum using ${sha_file}..."
    (cd "$(dirname "${SNAPSHOT_FILE}")" && sha256sum -c "$(basename "${sha_file}")" --status) || \
      err "SHA-256 verification FAILED for ${SNAPSHOT_FILE}!"
    log "Checksum OK."
  elif [[ "${SKIP_CHECKSUM}" == false ]]; then
    err "Checksum file ${sha_file} not found. Place it alongside the snapshot or pass --skip-checksum."
  else
    warn "Checksum file not found; proceeding due to --skip-checksum."
  fi

  local -a auth_header=()
  if [[ -n "${qdrant_api_key}" ]]; then
    auth_header=(-H "api-key: ${qdrant_api_key}")
  fi

  local enc_col
  enc_col=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "${COLLECTION_NAME}")

  log "Checking Qdrant readiness at ${qdrant_url}..."
  curl -sf "${qdrant_url}/readyz" >/dev/null || err "Qdrant is not reachable at ${qdrant_url}."

  log "Uploading and recovering snapshot into collection '${COLLECTION_NAME}'..."
  local upload_url="${qdrant_url}/collections/${enc_col}/snapshots/upload?priority=snapshot"
  local resp_tmp http_code
  resp_tmp=$(mktemp)
  http_code=$(curl -sS -o "${resp_tmp}" -w "%{http_code}" \
    -X POST "${upload_url}" \
    "${auth_header[@]}" \
    -F "snapshot=@${SNAPSHOT_FILE}")

  if [[ "${http_code}" != "200" ]]; then
    cat "${resp_tmp}" >&2 || true
    rm -f "${resp_tmp}"
    err "Snapshot upload failed with HTTP ${http_code}."
  fi
  rm -f "${resp_tmp}"

  log "Verifying collection '${COLLECTION_NAME}' status..."
  curl -sf "${auth_header[@]}" "${qdrant_url}/collections/${enc_col}" | \
    grep -q '"status":"ok"' || err "Collection '${COLLECTION_NAME}' did not report status ok."

  log "Successfully synchronized '${COLLECTION_NAME}' from ${SNAPSHOT_FILE}."
}

main "$@"
