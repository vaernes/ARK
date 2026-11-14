#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# ARK Company_Common Knowledge Base Snapshot Exporter
# Creates a Qdrant collection snapshot at HQ and writes <collection>.snapshot
# along with its <collection>.snapshot.sha256 verification file.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

OUTPUT_DIR="${REPO_ROOT}/artifacts/snapshots"
COLLECTION_NAME="Company_Common"
ENV_FILE="${REPO_ROOT}/.env"

log()  { printf '[%s] [INFO]  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
err()  { printf '[%s] [ERROR] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Exports a Qdrant collection snapshot + .sha256 file from the running ARK Qdrant instance.

Options:
  -o, --output-dir PATH    Destination directory (default: ${OUTPUT_DIR})
  -c, --collection NAME    Qdrant collection name (default: ${COLLECTION_NAME})
  -e, --env-file PATH      Path to .env file (default: ${REPO_ROOT}/.env)
  -h, --help               Show this help message and exit
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--output-dir)  OUTPUT_DIR="$2"; shift 2 ;;
    -c|--collection)  COLLECTION_NAME="$2"; shift 2 ;;
    -e|--env-file)    ENV_FILE="$2"; shift 2 ;;
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

  if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
  fi

  local qdrant_url="${QDRANT_URL:-http://127.0.0.1:6333}"
  local qdrant_api_key="${QDRANT_API_KEY:-}"
  mkdir -p "${OUTPUT_DIR}"

  local -a auth_header=()
  if [[ -n "${qdrant_api_key}" ]]; then
    auth_header=(-H "api-key: ${qdrant_api_key}")
  fi

  log "Checking Qdrant readiness at ${qdrant_url}..."
  curl -sf "${qdrant_url}/readyz" >/dev/null || err "Qdrant is not reachable at ${qdrant_url}."

  log "Creating Qdrant snapshot for collection '${COLLECTION_NAME}'..."
  local resp snap_name
  resp=$(curl -sf -X POST "${auth_header[@]}" "${qdrant_url}/collections/${COLLECTION_NAME}/snapshots?wait=true") || \
    err "Failed to trigger snapshot creation for collection '${COLLECTION_NAME}'."

  snap_name=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("result", {}).get("name", ""))' "${resp}")
  [[ -n "${snap_name}" ]] || err "Qdrant did not return a snapshot name: ${resp}"

  local out_file="${OUTPUT_DIR}/${COLLECTION_NAME}.snapshot"
  log "Downloading snapshot '${snap_name}' -> ${out_file}..."
  curl -sf "${auth_header[@]}" \
    "${qdrant_url}/collections/${COLLECTION_NAME}/snapshots/${snap_name}" \
    -o "${out_file}" || err "Failed to download snapshot '${snap_name}'."

  log "Generating SHA-256 checksum..."
  (cd "${OUTPUT_DIR}" && sha256sum "$(basename "${out_file}")" > "$(basename "${out_file}").sha256")

  log "Snapshot exported successfully: ${out_file} (checksum: ${out_file}.sha256)"
}

main "$@"
