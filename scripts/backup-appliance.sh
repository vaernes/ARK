#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${SCRIPT_DIR}/load-env.sh"

BACKUP=""
DATA_KEY=""
SECRETS_KEY=""
SIGNING_KEY=""

usage() {
  echo "Usage: bash scripts/backup-appliance.sh --backup PATH --data-key PUBLIC.pem --secrets-key PUBLIC.pem --signing-key PRIVATE.pem"
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup) BACKUP="$2"; shift 2 ;;
    --data-key) DATA_KEY="$2"; shift 2 ;;
    --secrets-key) SECRETS_KEY="$2"; shift 2 ;;
    --signing-key) SIGNING_KEY="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: Unknown argument $1" >&2; exit 1 ;;
  esac
done

[[ "${BACKUP}" == /* && -n "${DATA_KEY}" && -n "${SECRETS_KEY}" && -n "${SIGNING_KEY}" ]] || { usage >&2; exit 1; }
[[ ! -e "${BACKUP}" ]] || { echo "ERROR: Backup destination already exists." >&2; exit 1; }
[[ -f "${REPO_ROOT}/.env" ]] || { echo "ERROR: Missing .env." >&2; exit 1; }
load_ark_env "${REPO_ROOT}/.env"
for path in "${DATA_KEY}" "${SECRETS_KEY}" "${SIGNING_KEY}"; do
  [[ -f "${path}" ]] || { echo "ERROR: Key file not found: ${path}" >&2; exit 1; }
done
DATA_KEY="$(realpath "${DATA_KEY}")"
SECRETS_KEY="$(realpath "${SECRETS_KEY}")"
SIGNING_KEY="$(realpath "${SIGNING_KEY}")"
BACKUP_PARENT="$(realpath "$(dirname "${BACKUP}")")"
BACKUP_NAME="$(basename "${BACKUP}")"
[[ "${BACKUP_NAME}" != "." && "${BACKUP_NAME}" != ".." ]] || { echo "ERROR: Invalid backup name." >&2; exit 1; }
command -v zstd >/dev/null || { echo "ERROR: zstd is required." >&2; exit 1; }
docker compose version >/dev/null

compose() { (cd "${REPO_ROOT}" && docker compose --profile local-llm "$@"); }
running_text="$(compose ps --services --filter status=running)"
image_text="$(compose images --quiet)"
mapfile -t running <<< "${running_text}"
mapfile -t images < <(printf '%s\n' "${image_text}" | sed '/^$/d' | sort -u)
[[ ${#images[@]} -gt 0 ]] || { echo "ERROR: No appliance images found." >&2; exit 1; }
if [[ -z "${running_text}" ]]; then running=(); fi
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ark-cold-backup-XXXXXX")"
QUIESCED=false
cleanup() {
  local status=$?
  trap - EXIT
  if [[ "${QUIESCED}" == true && ${#running[@]} -gt 0 ]]; then
    compose start "${running[@]}" || { echo "ERROR: Failed to restart previously running services." >&2; status=1; }
  fi
  rm -f -- "${WORK_DIR}/images.tar.zst"
  rmdir -- "${WORK_DIR}" || { echo "ERROR: Could not remove temporary backup directory ${WORK_DIR}." >&2; status=1; }
  exit "${status}"
}
trap cleanup EXIT

QUIESCED=true
compose stop
remaining="$(compose ps --services --filter status=running)"
[[ -z "${remaining}" ]] || { echo "ERROR: Services are still running: ${remaining}" >&2; exit 1; }
docker save "${images[@]}" | zstd -T0 -3 -o "${WORK_DIR}/images.tar.zst"

docker run --rm --network none \
  -v "${REPO_ROOT}:/workspace:ro" -v "${BACKUP_PARENT}:/backup" -v "${WORK_DIR}:/work:ro" \
  -v "${DATA_KEY}:/keys/data.pem:ro" -v "${SECRETS_KEY}:/keys/secrets.pem:ro" \
  -v "${SIGNING_KEY}:/keys/signing.pem:ro" \
  "${ARK_ADMIN_IMAGE:-ark-admin:latest}" \
  python3 /workspace/scripts/appliance_backup.py create --root /workspace \
  --backup "/backup/${BACKUP_NAME}" --images /work/images.tar.zst \
  --data-key /keys/data.pem --secrets-key /keys/secrets.pem --signing-key /keys/signing.pem \
  --owner-uid "$(id -u)" --owner-gid "$(id -g)"
echo "Encrypted appliance backup created at ${BACKUP}."
