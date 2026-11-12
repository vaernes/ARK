#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# ARK Project Knowledge Base Snapshot Exporter
# Creates a Qdrant collection snapshot at HQ and writes <collection>.snapshot
# (or the explicit --out file) along with its .sha256 verification file.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

OUTPUT_DIR="${REPO_ROOT}/artifacts/snapshots"
OUT_ARG=""
COLLECTION_NAME=""
ENV_FILE="${REPO_ROOT}/.env"

log()  { printf '[%s] [INFO]  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
err()  { printf '[%s] [ERROR] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: $(basename "$0") --collection NAME [OPTIONS]

Exports a Qdrant collection snapshot + .sha256 file from the running ARK Qdrant instance.

Options:
  -c, --collection NAME    Qdrant collection name (required, e.g. Project_Alpha)
  -o, --output-dir PATH    Destination directory (default: ${OUTPUT_DIR})
      --out PATH           Destination snapshot file path (e.g. ./Shared_KB.snapshot) or directory
  -e, --env-file PATH      Path to .env file (default: ${REPO_ROOT}/.env)
  -h, --help               Show this help message and exit
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--output-dir)  [[ $# -ge 2 ]] || err "Missing value for $1"; OUTPUT_DIR="$2"; shift 2 ;;
    --out)            [[ $# -ge 2 ]] || err "Missing value for $1"; OUT_ARG="$2"; shift 2 ;;
    -c|--collection)  [[ $# -ge 2 ]] || err "Missing value for $1"; COLLECTION_NAME="$2"; shift 2 ;;
    -e|--env-file)    [[ $# -ge 2 ]] || err "Missing value for $1"; ENV_FILE="$2"; shift 2 ;;
    -h|--help)        usage; exit 0 ;;
    *)                err "Unknown argument: $1" ;;
  esac
done

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || err "Required command not found: $1"
}

main() {
  require_cmd curl
  require_cmd python3
  require_cmd sha256sum

  [[ -n "${COLLECTION_NAME}" ]] || { usage; err "--collection NAME is required."; }

  if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
  fi

  local qdrant_url="${QDRANT_URL:-http://127.0.0.1:6333}"
  qdrant_url="${qdrant_url%/}"
  local qdrant_api_key="${QDRANT_API_KEY:-}"

  local out_file
  if [[ -n "${OUT_ARG}" ]]; then
    if [[ "${OUT_ARG}" == *.snapshot || ( ! -d "${OUT_ARG}" && -d "$(dirname "${OUT_ARG}")" ) ]]; then
      out_file="${OUT_ARG}"
      mkdir -p "$(dirname "${out_file}")"
    else
      mkdir -p "${OUT_ARG}"
      out_file="${OUT_ARG%/}/${COLLECTION_NAME}.snapshot"
    fi
  else
    mkdir -p "${OUTPUT_DIR}"
    out_file="${OUTPUT_DIR%/}/${COLLECTION_NAME}.snapshot"
  fi

  local -a auth_header=()
  if [[ -n "${qdrant_api_key}" ]]; then
    auth_header=(-H "api-key: ${qdrant_api_key}")
  fi

  local enc_col
  enc_col=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "${COLLECTION_NAME}")

  log "Checking Qdrant readiness at ${qdrant_url}..."
  curl -sf "${qdrant_url}/readyz" >/dev/null || err "Qdrant is not reachable at ${qdrant_url}."

  log "Creating Qdrant snapshot for collection '${COLLECTION_NAME}'..."
  local resp snap_name
  resp=$(curl -sf -X POST "${auth_header[@]}" "${qdrant_url}/collections/${enc_col}/snapshots?wait=true") || \
    err "Failed to trigger snapshot creation for collection '${COLLECTION_NAME}'."

  snap_name=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("result", {}).get("name", ""))' "${resp}")
  [[ -n "${snap_name}" ]] || err "Qdrant did not return a snapshot name: ${resp}"

  local enc_snap
  enc_snap=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "${snap_name}")

  log "Downloading snapshot '${snap_name}' -> ${out_file}..."
  curl -sf "${auth_header[@]}" \
    "${qdrant_url}/collections/${enc_col}/snapshots/${enc_snap}" \
    -o "${out_file}" || err "Failed to download snapshot '${snap_name}'."

  log "Generating SHA-256 checksum..."
  (cd "$(dirname "${out_file}")" && sha256sum "$(basename "${out_file}")" > "$(basename "${out_file}").sha256")

  log "Snapshot exported successfully: ${out_file} (checksum: ${out_file}.sha256)"
}

main "$@"
