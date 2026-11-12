#!/usr/bin/env bash
# ==============================================================================
# ARK (Air-gapped Retrieval & Knowledge) - Offline LLM & Model Update Tool
# ==============================================================================
# Safely stages new offline model weights (LLM, Embedding, or Reranker) into
# ./artifacts/models/, updates .env atomically with a timestamped backup,
# recreates only the affected inference container, and verifies health.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ARK_VERSION="$(tr -d '[:space:]' < "${REPO_ROOT}/VERSION" 2>/dev/null || echo '0.0.0-dev')"
ENV_FILE="${REPO_ROOT}/.env"
MODELS_ROOT="${REPO_ROOT}/artifacts/models"
BACKUP_ENV_DIR="${REPO_ROOT}/data/backups/env"

MODEL_TYPE="llm"
MODEL_DIR=""
MODEL_ID=""
SERVED_NAME=""
ARCHIVE_PATH=""
SRC_PATH=""
MAX_MODEL_LEN=""
GPU_MEM=""
TP_SIZE=""
LIST_ONLY=false
ROLLBACK=false
NO_RESTART=false
DRY_RUN=false

log()  { printf '[ark-update-llm] %s\n' "$*"; }
warn() { printf '[ark-update-llm] [WARN] %s\n' "$*" >&2; }
err()  { printf '[ark-update-llm] [ERROR] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: ./scripts/update-llm.sh [OPTIONS]

Update or switch the active LLM, Embedding, or Reranker model in an air-gapped
ARK deployment without touching other services.

Options:
  --type <llm|embed|rerank>   Model service to update (default: llm)
  --model-dir <DIR>           Target directory name under ./artifacts/models/
  --model-id <HF_ID>          Optional Hugging Face ID metadata for .env
  --served-name <NAME>        Optional served model alias for vLLM (llm only)
  --archive <PATH>            Unpack offline model archive (.tar.zst/.tar.gz/.tar)
  --src <PATH>                Copy model directory from local/USB path
  --max-model-len <N>         Update VLLM_MAX_MODEL_LEN in .env (llm only)
  --gpu-mem <FLOAT>           Update VLLM_GPU_MEMORY_UTILIZATION in .env (llm only)
  --tp <N>                    Update VLLM_TENSOR_PARALLEL_SIZE in .env (llm only)
  --list                      List local models in ./artifacts/models/ and active .env
  --rollback                  Restore the most recent .env backup and recreate service
  --no-restart                Stage weights and update .env without recreating container
  --dry-run                   Validate inputs and print planned changes only
  -V, --version               Print ARK version and exit
  -h, --help                  Show this help message

Examples:
  ./scripts/update-llm.sh --list
  ./scripts/update-llm.sh --archive /mnt/usb/Qwen2.5-32B.tar.zst --model-dir Qwen--Qwen2.5-32B-Instruct
  ./scripts/update-llm.sh --model-dir Qwen--Qwen2.5-32B-Instruct --max-model-len 32768 --tp 2
  ./scripts/update-llm.sh --type embed --model-dir BAAI--bge-large-en-v1.5 --model-id BAAI/bge-large-en-v1.5
  ./scripts/update-llm.sh --rollback
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --type)          MODEL_TYPE="$2"; shift 2 ;;
      --model-dir)     MODEL_DIR="$2"; shift 2 ;;
      --model-id)      MODEL_ID="$2"; shift 2 ;;
      --served-name)   SERVED_NAME="$2"; shift 2 ;;
      --archive)       ARCHIVE_PATH="$2"; shift 2 ;;
      --src)           SRC_PATH="$2"; shift 2 ;;
      --max-model-len) MAX_MODEL_LEN="$2"; shift 2 ;;
      --gpu-mem)       GPU_MEM="$2"; shift 2 ;;
      --tp)            TP_SIZE="$2"; shift 2 ;;
      --list)          LIST_ONLY=true; shift ;;
      --rollback)      ROLLBACK=true; shift ;;
      --no-restart)    NO_RESTART=true; shift ;;
      --dry-run)       DRY_RUN=true; shift ;;
      -V|--version)    printf 'ARK v%s\n' "${ARK_VERSION}"; exit 0 ;;
      -h|--help)       usage; exit 0 ;;
      *)               err "Unknown option: $1 (use --help for usage)" ;;
    esac
  done
}

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    err "Neither 'docker compose' nor 'docker-compose' is available."
  fi
}

get_env_val() {
  local key="$1"
  local default_val="${2:-}"
  if [[ -f "${ENV_FILE}" ]]; then
    local val
    val=$(grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 | cut -d'=' -f2- || true)
    if [[ -n "${val}" ]]; then
      printf '%s' "${val}"
      return
    fi
  fi
  printf '%s' "${default_val}"
}

set_env_val() {
  local file="$1"
  local key="$2"
  local val="$3"
  python3 - "$file" "$key" "$val" <<'PYEOF'
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
val = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
out = []
found = False
for line in lines:
    if line.startswith(f"{key}="):
        out.append(f"{key}={val}\n")
        found = True
    else:
        out.append(line)
if not found:
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    out.append(f"{key}={val}\n")
path.write_text("".join(out), encoding="utf-8")
PYEOF
}

service_for_type() {
  case "${MODEL_TYPE}" in
    llm)    printf 'vllm' ;;
    embed)  printf 'tei-embed' ;;
    rerank) printf 'tei-rerank' ;;
    *)      err "Invalid --type '${MODEL_TYPE}'. Must be llm, embed, or rerank." ;;
  esac
}

list_models() {
  log "Active .env model configuration:"
  printf '  LLM_MODEL_DIR=%s (LLM_MODEL_ID=%s, LLM_SERVED_NAME=%s)\n' \
    "$(get_env_val LLM_MODEL_DIR Qwen--Qwen2.5-14B-Instruct)" \
    "$(get_env_val LLM_MODEL_ID Qwen/Qwen2.5-14B-Instruct)" \
    "$(get_env_val LLM_SERVED_NAME ark-llm)"
  printf '  EMBED_MODEL_DIR=%s (EMBED_MODEL_ID=%s)\n' \
    "$(get_env_val EMBED_MODEL_DIR BAAI--bge-m3)" \
    "$(get_env_val EMBED_MODEL_ID BAAI/bge-m3)"
  printf '  RERANK_MODEL_DIR=%s (RERANK_MODEL_ID=%s)\n' \
    "$(get_env_val RERANK_MODEL_DIR BAAI--bge-reranker-v2-m3)" \
    "$(get_env_val RERANK_MODEL_ID BAAI/bge-reranker-v2-m3)"
  printf '\n'

  if [[ ! -d "${MODELS_ROOT}" ]]; then
    warn "Models directory does not exist yet: ${MODELS_ROOT}"
    return 0
  fi

  log "Local models in ${MODELS_ROOT}:"
  python3 - "${MODELS_ROOT}" <<'PYEOF'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
dirs = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
if not dirs:
    print("  (none)")
    sys.exit(0)

for d in dirs:
    size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    weights = sum(1 for f in d.rglob("*") if f.is_file() and f.suffix in {".safetensors", ".bin", ".gguf", ".pt"})
    cfg_path = d / "config.json"
    arch = "-"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            archs = cfg.get("architectures") or []
            arch = archs[0] if archs else cfg.get("model_type", "-")
        except Exception:
            arch = "invalid-config"
    gb = size / (1024 ** 3)
    print(f"  - {d.name:<40} | {gb:6.2f} GiB | weights={weights:<3} | arch={arch}")
PYEOF
}

verify_model_dir() {
  local target_dir="$1"
  [[ -d "${target_dir}" ]] || err "Model directory does not exist: ${target_dir}"
  [[ -f "${target_dir}/config.json" ]] || warn "No config.json found in ${target_dir} (non-HF or GGUF layout?)."

  local weight_count
  weight_count=$(find "${target_dir}" -maxdepth 3 -type f \( -name '*.safetensors' -o -name '*.bin' -o -name '*.gguf' -o -name '*.pt' \) | wc -l)
  if [[ "${weight_count}" -eq 0 ]]; then
    err "No model weight files (*.safetensors, *.bin, *.gguf, *.pt) found in ${target_dir}"
  fi
  log "Verified ${weight_count} weight file(s) in ${target_dir}."
}

stage_archive() {
  local archive="$1"
  [[ -f "${archive}" ]] || err "Archive not found: ${archive}"

  if [[ -f "${archive}.sha256" ]]; then
    log "Verifying SHA-256 checksum for ${archive}..."
    (cd "$(dirname "${archive}")" && sha256sum -c "$(basename "${archive}.sha256")")
  fi

  if [[ "${DRY_RUN}" == true ]]; then
    log "[DRY-RUN] Would unpack ${archive} into ${MODELS_ROOT}/"
    return 0
  fi

  mkdir -p "${MODELS_ROOT}"
  case "${archive}" in
    *.tar.zst|*.tzst)
      command -v zstd >/dev/null 2>&1 || err "zstd is required to unpack ${archive}"
      zstd -d -c "${archive}" | tar -xf - -C "${MODELS_ROOT}"
      ;;
    *.tar.gz|*.tgz)
      tar -xzf "${archive}" -C "${MODELS_ROOT}"
      ;;
    *.tar)
      tar -xf "${archive}" -C "${MODELS_ROOT}"
      ;;
    *)
      err "Unsupported archive format: ${archive} (expected .tar.zst, .tar.gz, .tgz, or .tar)"
      ;;
  esac
  log "Unpacked ${archive} into ${MODELS_ROOT}/"
}

stage_src() {
  local src="$1"
  [[ -d "${src}" ]] || err "Source directory does not exist: ${src}"
  [[ -n "${MODEL_DIR}" ]] || MODEL_DIR="$(basename "${src}")"

  local dest="${MODELS_ROOT}/${MODEL_DIR}"
  if [[ "${DRY_RUN}" == true ]]; then
    log "[DRY-RUN] Would copy ${src} -> ${dest}"
    return 0
  fi

  mkdir -p "${dest}"
  cp -a "${src}/." "${dest}/"
  log "Copied model weights from ${src} -> ${dest}"
}

wait_for_service_health() {
  local service="$1"
  local container="ark-${service}"
  log "Waiting for ${container} to report healthy (timeout: 300s)..."

  local elapsed=0
  while [[ ${elapsed} -lt 300 ]]; do
    local status
    status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container}" 2>/dev/null || echo "missing")
    if [[ "${status}" == "healthy" ]]; then
      log "${container} is healthy."
      return 0
    fi
    if [[ "${status}" == "exited" || "${status}" == "dead" ]]; then
      docker logs --tail 40 "${container}" >&2 || true
      err "${container} exited unexpectedly (status=${status}). Use './scripts/update-llm.sh --rollback' to revert."
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done

  docker logs --tail 40 "${container}" >&2 || true
  err "Timed out waiting for ${container} to become healthy."
}

do_rollback() {
  [[ -d "${BACKUP_ENV_DIR}" ]] || err "No .env backups found in ${BACKUP_ENV_DIR}"
  local latest
  latest=$(ls -1t "${BACKUP_ENV_DIR}"/.env.bak.* 2>/dev/null | head -n 1 || true)
  [[ -n "${latest}" ]] || err "No .env backup files found in ${BACKUP_ENV_DIR}"

  local service
  service="$(service_for_type)"

  if [[ "${DRY_RUN}" == true ]]; then
    log "[DRY-RUN] Would restore ${latest} -> ${ENV_FILE} and recreate ${service}"
    return 0
  fi

  cp -a "${latest}" "${ENV_FILE}"
  log "Restored ${ENV_FILE} from ${latest}"
  if [[ "${NO_RESTART}" == false ]]; then
    (cd "${REPO_ROOT}" && compose_cmd up -d --no-deps --force-recreate "${service}")
    wait_for_service_health "${service}"
  fi
}

main() {
  parse_args "$@"

  if [[ "${LIST_ONLY}" == true ]]; then
    list_models
    exit 0
  fi

  if [[ "${ROLLBACK}" == true ]]; then
    do_rollback
    exit 0
  fi

  [[ -f "${ENV_FILE}" ]] || err "Missing ${ENV_FILE}. Copy .env.example to .env first."

  if [[ -n "${ARCHIVE_PATH}" ]]; then
    stage_archive "${ARCHIVE_PATH}"
  fi
  if [[ -n "${SRC_PATH}" ]]; then
    stage_src "${SRC_PATH}"
  fi

  [[ -n "${MODEL_DIR}" ]] || err "--model-dir is required (or use --list / --rollback)."
  if [[ "${MODEL_DIR}" == *"/"* || "${MODEL_DIR}" == *".."* ]]; then
    err "--model-dir must be a single directory name under ${MODELS_ROOT}, not a path: ${MODEL_DIR}"
  fi

  local target_path="${MODELS_ROOT}/${MODEL_DIR}"
  if [[ "${DRY_RUN}" == false || -d "${target_path}" ]]; then
    verify_model_dir "${target_path}"
  fi

  local service
  service="$(service_for_type)"

  if [[ "${DRY_RUN}" == true ]]; then
    log "[DRY-RUN] Target service: ${service}"
    log "[DRY-RUN] Would backup ${ENV_FILE} and set model dir to ${MODEL_DIR}"
    [[ -n "${MODEL_ID}" ]] && log "[DRY-RUN] Would set model ID to ${MODEL_ID}"
    [[ -n "${SERVED_NAME}" ]] && log "[DRY-RUN] Would set LLM_SERVED_NAME=${SERVED_NAME}"
    [[ -n "${MAX_MODEL_LEN}" ]] && log "[DRY-RUN] Would set VLLM_MAX_MODEL_LEN=${MAX_MODEL_LEN}"
    [[ -n "${GPU_MEM}" ]] && log "[DRY-RUN] Would set VLLM_GPU_MEMORY_UTILIZATION=${GPU_MEM}"
    [[ -n "${TP_SIZE}" ]] && log "[DRY-RUN] Would set VLLM_TENSOR_PARALLEL_SIZE=${TP_SIZE}"
    [[ "${NO_RESTART}" == false ]] && log "[DRY-RUN] Would run: docker compose up -d --no-deps --force-recreate ${service}"
    exit 0
  fi

  mkdir -p "${BACKUP_ENV_DIR}"
  local stamp backup_path
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_path="${BACKUP_ENV_DIR}/.env.bak.${stamp}"
  cp -a "${ENV_FILE}" "${backup_path}"
  log "Backed up .env -> ${backup_path}"

  case "${MODEL_TYPE}" in
    llm)
      set_env_val "${ENV_FILE}" "LLM_MODEL_DIR" "${MODEL_DIR}"
      [[ -n "${MODEL_ID}" ]] && set_env_val "${ENV_FILE}" "LLM_MODEL_ID" "${MODEL_ID}"
      [[ -n "${SERVED_NAME}" ]] && set_env_val "${ENV_FILE}" "LLM_SERVED_NAME" "${SERVED_NAME}"
      [[ -n "${MAX_MODEL_LEN}" ]] && set_env_val "${ENV_FILE}" "VLLM_MAX_MODEL_LEN" "${MAX_MODEL_LEN}"
      [[ -n "${GPU_MEM}" ]] && set_env_val "${ENV_FILE}" "VLLM_GPU_MEMORY_UTILIZATION" "${GPU_MEM}"
      [[ -n "${TP_SIZE}" ]] && set_env_val "${ENV_FILE}" "VLLM_TENSOR_PARALLEL_SIZE" "${TP_SIZE}"
      ;;
    embed)
      set_env_val "${ENV_FILE}" "EMBED_MODEL_DIR" "${MODEL_DIR}"
      [[ -n "${MODEL_ID}" ]] && set_env_val "${ENV_FILE}" "EMBED_MODEL_ID" "${MODEL_ID}"
      ;;
    rerank)
      set_env_val "${ENV_FILE}" "RERANK_MODEL_DIR" "${MODEL_DIR}"
      [[ -n "${MODEL_ID}" ]] && set_env_val "${ENV_FILE}" "RERANK_MODEL_ID" "${MODEL_ID}"
      ;;
  esac

  log "Updated ${ENV_FILE} for ${MODEL_TYPE} (${MODEL_DIR})."

  if [[ "${NO_RESTART}" == false ]]; then
    log "Recreating ${service} container..."
    (cd "${REPO_ROOT}" && compose_cmd up -d --no-deps --force-recreate "${service}")
    wait_for_service_health "${service}"
  else
    log "Skipped container restart (--no-restart). Run 'docker compose up -d --no-deps --force-recreate ${service}' when ready."
  fi

  log "Model update complete!"
}

main "$@"
