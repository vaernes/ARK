#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP=""
TARGET=""
DATA_KEY=""
SECRETS_KEY=""
VERIFY_KEY=""
WORK_DIR=""
VERIFY_ONLY=false
IMAGE="${ARK_ADMIN_IMAGE:-ark-admin:latest}"

usage() {
  echo "Usage: bash scripts/restore-appliance.sh --backup PATH --data-key PRIVATE.pem --secrets-key PRIVATE.pem --verify-key PUBLIC.pem [--target NEW_PATH | --verify-only] [--work-dir PATH]"
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup) BACKUP="$2"; shift 2 ;;
    --target) TARGET="$2"; shift 2 ;;
    --data-key) DATA_KEY="$2"; shift 2 ;;
    --secrets-key) SECRETS_KEY="$2"; shift 2 ;;
    --verify-key) VERIFY_KEY="$2"; shift 2 ;;
    --work-dir) WORK_DIR="$2"; shift 2 ;;
    --verify-only) VERIFY_ONLY=true; shift ;;
    --image) IMAGE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: Unknown argument $1" >&2; exit 1 ;;
  esac
done
[[ -d "${BACKUP}" && -n "${DATA_KEY}" && -n "${SECRETS_KEY}" && -n "${VERIFY_KEY}" ]] || { usage >&2; exit 1; }
for path in "${DATA_KEY}" "${SECRETS_KEY}" "${VERIFY_KEY}"; do
  [[ -f "${path}" ]] || { echo "ERROR: Key file not found: ${path}" >&2; exit 1; }
done
BACKUP="$(realpath "${BACKUP}")"
DATA_KEY="$(realpath "${DATA_KEY}")"
SECRETS_KEY="$(realpath "${SECRETS_KEY}")"
VERIFY_KEY="$(realpath "${VERIFY_KEY}")"
args=(verify)
mounts=()
if [[ "${VERIFY_ONLY}" != true ]]; then
  [[ "${TARGET}" == /* && ! -e "${TARGET}" ]] || { echo "ERROR: --target must be a new absolute path." >&2; exit 1; }
  TARGET_PARENT="$(realpath "$(dirname "${TARGET}")")"
  TARGET_NAME="$(basename "${TARGET}")"
  [[ "${TARGET_NAME}" != "." && "${TARGET_NAME}" != ".." ]] || { echo "ERROR: Invalid target name." >&2; exit 1; }
  mounts=(-v "${TARGET_PARENT}:/restore")
  args=(restore --target "/restore/${TARGET_NAME}")
fi
if [[ -n "${WORK_DIR}" ]]; then
  [[ -d "${WORK_DIR}" ]] || { echo "ERROR: Recovery work directory does not exist." >&2; exit 1; }
  WORK_DIR="$(realpath "${WORK_DIR}")"
  mounts+=(-v "${WORK_DIR}:/work")
  args+=(--work-dir /work)
fi

docker run --rm --network none \
  -v "${REPO_ROOT}:/workspace:ro" -v "${BACKUP}:/backup:ro" "${mounts[@]}" \
  -v "${DATA_KEY}:/keys/data.pem:ro" -v "${SECRETS_KEY}:/keys/secrets.pem:ro" \
  -v "${VERIFY_KEY}:/keys/verify.pem:ro" \
  "${IMAGE}" python3 /workspace/scripts/appliance_backup.py "${args[@]}" \
  --backup /backup --data-key /keys/data.pem --secrets-key /keys/secrets.pem --verify-key /keys/verify.pem \
  --owner-uid "$(id -u)" --owner-gid "$(id -g)"
if [[ "${VERIFY_ONLY}" != true ]]; then
  echo "Restore complete at ${TARGET}. Load artifacts/ark-recovery-images.tar.zst and start services only after reviewing the restored configuration."
fi
