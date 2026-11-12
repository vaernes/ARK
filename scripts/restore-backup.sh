#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# ARK Full-Stack Backup Verification & Disaster Recovery Restore
# Verifies manifest.json SHA-256 checksums and gzip integrity, then restores
# PostgreSQL databases (openwebui, keycloak, ark_admin) and Qdrant snapshots.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

BACKUP_DIR=""
ENV_FILE="${REPO_ROOT}/.env"
POSTGRES_CONTAINER="${ARK_POSTGRES_CONTAINER:-ark-postgres}"
VERIFY_ONLY=false
SKIP_DB=false
SKIP_QDRANT=false

log()  { printf '[%s] [INFO]  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
warn() { printf '[%s] [WARN]  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
err()  { printf '[%s] [ERROR] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: $(basename "$0") --backup-dir PATH [OPTIONS]

Verifies and restores a full ARK backup bundle (PostgreSQL + Qdrant).

Options:
  -b, --backup-dir PATH         Path to ark-backup-<timestamp> directory (required)
  -e, --env-file PATH           Path to .env file (default: ${REPO_ROOT}/.env)
      --postgres-container NAME Docker container name for Postgres (default: ${POSTGRES_CONTAINER})
      --verify-only             Verify manifest checksums and gzip integrity without restoring
      --skip-db                 Skip PostgreSQL restore
      --skip-qdrant             Skip Qdrant snapshot restore
  -h, --help                    Show this help message and exit
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -b|--backup-dir)         BACKUP_DIR="$2"; shift 2 ;;
    -e|--env-file)           ENV_FILE="$2"; shift 2 ;;
    --postgres-container)    POSTGRES_CONTAINER="$2"; shift 2 ;;
    --verify-only)           VERIFY_ONLY=true; shift ;;
    --skip-db)               SKIP_DB=true; shift ;;
    --skip-qdrant)           SKIP_QDRANT=true; shift ;;
    -h|--help)               usage; exit 0 ;;
    *)                       err "Unknown argument: $1" ;;
  esac
done

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || err "Required command not found: $1"
}

main() {
  require_cmd python3
  require_cmd gzip
  require_cmd curl

  [[ -n "${BACKUP_DIR}" ]] || { usage; err "--backup-dir PATH is required."; }
  [[ -d "${BACKUP_DIR}" ]] || err "Backup directory not found: ${BACKUP_DIR}"
  [[ -f "${BACKUP_DIR}/manifest.json" ]] || err "Backup manifest missing: ${BACKUP_DIR}/manifest.json"

  if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
  fi

  local pg_user="${POSTGRES_USER:-ark_webui}"
  local qdrant_url="${QDRANT_URL:-http://127.0.0.1:6333}"
  local qdrant_api_key="${QDRANT_API_KEY:-}"

  log "Verifying backup manifest checksums and gzip archives in ${BACKUP_DIR}..."
  python3 - "${BACKUP_DIR}" "${REPO_ROOT}/admin" <<'PY'
import sys
from pathlib import Path

backup_dir = Path(sys.argv[1])
sys.path.insert(0, sys.argv[2])
from backup_utils import verify_backup_manifest

manifest = verify_backup_manifest(backup_dir)
print(
    f"Verified manifest: {len(manifest.get('databases', []))} databases, "
    f"{len(manifest.get('qdrant_snapshots', []))} Qdrant snapshots."
)
PY

  if [[ "${VERIFY_ONLY}" == true ]]; then
    log "Verification complete (--verify-only). No changes made."
    exit 0
  fi

  if [[ "${SKIP_QDRANT}" == false ]]; then
    local -a auth_header=()
    if [[ -n "${qdrant_api_key}" ]]; then
      auth_header=(-H "api-key: ${qdrant_api_key}")
    fi

    log "Checking Qdrant readiness at ${qdrant_url}..."
    curl -sf "${qdrant_url%/}/readyz" >/dev/null || err "Qdrant is not reachable at ${qdrant_url}."

    while IFS=$'\t' read -r col_name rel_path; do
      [[ -n "${col_name}" ]] || continue
      local snap_file="${BACKUP_DIR}/${rel_path}"
      log "Restoring Qdrant collection '${col_name}' from ${rel_path}..."
      local enc_col
      enc_col="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "${col_name}")"
      local http_code
      http_code=$(curl -sS -o /tmp/ark_qdrant_restore.json -w "%{http_code}" \
        -X POST "${qdrant_url%/}/collections/${enc_col}/snapshots/upload?priority=snapshot" \
        "${auth_header[@]}" \
        -F "snapshot=@${snap_file}")
      if [[ "${http_code}" != "200" ]]; then
        cat /tmp/ark_qdrant_restore.json >&2 || true
        rm -f /tmp/ark_qdrant_restore.json
        err "Qdrant restore failed for '${col_name}' with HTTP ${http_code}."
      fi
      rm -f /tmp/ark_qdrant_restore.json
      curl -sf "${auth_header[@]}" "${qdrant_url%/}/collections/${enc_col}" | grep -q '"status":"ok"' || \
        err "Collection '${col_name}' did not report status ok after restore."
    done < <(python3 -c '
import json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
for s in m.get("qdrant_snapshots", []):
    print(f"{s[\"collection\"]}\t{s[\"path\"]}")
' "${BACKUP_DIR}/manifest.json")
    log "All Qdrant snapshots restored and verified."
  fi

  if [[ "${SKIP_DB}" == false ]]; then
    require_cmd docker
    while IFS=$'\t' read -r db_name rel_file; do
      [[ -n "${db_name}" ]] || continue
      local dump_file="${BACKUP_DIR}/${rel_file}"
      log "Restoring PostgreSQL database '${db_name}' from ${rel_file}..."
      gzip -dc "${dump_file}" | docker exec -i "${POSTGRES_CONTAINER}" \
        psql -U "${pg_user}" -d "${db_name}" -v ON_ERROR_STOP=1 >/dev/null
      docker exec "${POSTGRES_CONTAINER}" \
        psql -U "${pg_user}" -d "${db_name}" -tAc "SELECT 1" | grep -q "^1$" || \
        err "PostgreSQL post-restore check failed for '${db_name}'."
    done < <(python3 -c '
import json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
for d in m.get("databases", []):
    print(f"{d[\"database\"]}\t{d[\"file\"]}")
' "${BACKUP_DIR}/manifest.json")
    log "All PostgreSQL databases restored and verified."
  fi

  log "Full-stack restore completed successfully from ${BACKUP_DIR}."
}

main "$@"
