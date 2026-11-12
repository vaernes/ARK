#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# ARK Offline Bundle Builder (Run at Connected HQ)
# Pulls pinned Docker images, saves & compresses them with zstd, downloads
# Hugging Face model weights into ./artifacts/models/, and generates sha256sums.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ARK_VERSION="$(tr -d '[:space:]' < "${REPO_ROOT}/VERSION" 2>/dev/null || echo '0.0.0-dev')"

ENV_FILE="${REPO_ROOT}/.env"
ARTIFACTS_DIR="${REPO_ROOT}/artifacts"
MIN_FREE_GB=100
SKIP_IMAGES=false
SKIP_MODELS=false
PACKAGE_MODELS=false

log()  { printf '[%s] [INFO]  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
warn() { printf '[%s] [WARN]  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
err()  { printf '[%s] [ERROR] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Packages ARK Docker images and Hugging Face models for air-gapped transfer.

Options:
  -e, --env-file PATH     Path to .env file (default: ${REPO_ROOT}/.env, falls back to .env.example)
  -o, --output-dir PATH   Artifacts output directory (default: ${REPO_ROOT}/artifacts)
  -m, --min-free-gb N     Minimum free disk space required in GB (default: ${MIN_FREE_GB})
      --package-models    Also archive ./artifacts/models into models.tar.zst + sha256
      --skip-images       Skip pulling and saving Docker images
      --skip-models       Skip downloading Hugging Face models
  -V, --version           Print ARK version and exit
  -h, --help              Show this help message and exit
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -e|--env-file)       ENV_FILE="$2"; shift 2 ;;
    -o|--output-dir)     ARTIFACTS_DIR="$2"; shift 2 ;;
    -m|--min-free-gb)    MIN_FREE_GB="$2"; shift 2 ;;
    --package-models)    PACKAGE_MODELS=true; shift ;;
    --skip-images)       SKIP_IMAGES=true; shift ;;
    --skip-models)       SKIP_MODELS=true; shift ;;
    -V|--version)        printf 'ARK v%s\n' "${ARK_VERSION}"; exit 0 ;;
    -h|--help)           usage; exit 0 ;;
    *)                   err "Unknown argument: $1" ;;
  esac
done

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || err "Required command not found: $1"
}

check_disk_space() {
  local target_dir="$1"
  local min_gb="$2"
  mkdir -p "${target_dir}"
  local avail_kb
  avail_kb=$(df -Pk "${target_dir}" | awk 'NR==2 {print $4}')
  local avail_gb=$(( avail_kb / 1024 / 1024 ))
  log "Disk space check on ${target_dir}: ${avail_gb} GB available (minimum ${min_gb} GB)."
  if (( avail_gb < min_gb )); then
    err "Insufficient disk space on ${target_dir}: ${avail_gb} GB < ${min_gb} GB required."
  fi
}

load_env() {
  local source_env="${ENV_FILE}"
  if [[ ! -f "${source_env}" ]]; then
    if [[ -f "${REPO_ROOT}/.env.example" ]]; then
      warn "${source_env} not found; falling back to ${REPO_ROOT}/.env.example"
      source_env="${REPO_ROOT}/.env.example"
    else
      err "Neither ${ENV_FILE} nor .env.example exists."
    fi
  fi

  set -a
  # shellcheck disable=SC1090
  source "${source_env}"
  set +a

  VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.6.4.post1}"
  TEI_IMAGE="${TEI_IMAGE:-ghcr.io/huggingface/text-embeddings-inference:1.6}"
  QDRANT_IMAGE="${QDRANT_IMAGE:-qdrant/qdrant:v1.12.5}"
  OPEN_WEBUI_IMAGE="${OPEN_WEBUI_IMAGE:-ghcr.io/open-webui/open-webui:v0.5.10}"
  POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:16-alpine}"
  TIKA_IMAGE="${TIKA_IMAGE:-apache/tika:3.1.0.0-full}"
  TIKA_IMAGE_DIGEST="${TIKA_IMAGE_DIGEST:-sha256:1221afa48af9158e14b8d005bbcfa49f3d7fc4e5113db48cad586955bc64992b}"
  CADDY_IMAGE="${CADDY_IMAGE:-caddy:2-alpine}"
  KEYCLOAK_IMAGE="${KEYCLOAK_IMAGE:-quay.io/keycloak/keycloak:26.0}"
  OAUTH2_PROXY_IMAGE="${OAUTH2_PROXY_IMAGE:-quay.io/oauth2-proxy/oauth2-proxy:v7.7.1}"
  ARK_ADMIN_IMAGE="${ARK_ADMIN_IMAGE:-ark-admin:latest}"

  LLM_MODEL_ID="${LLM_MODEL_ID:-Qwen/Qwen2.5-14B-Instruct}"
  LLM_MODEL_DIR="${LLM_MODEL_DIR:-Qwen--Qwen2.5-14B-Instruct}"
  EMBED_MODEL_ID="${EMBED_MODEL_ID:-BAAI/bge-m3}"
  EMBED_MODEL_DIR="${EMBED_MODEL_DIR:-BAAI--bge-m3}"
  RERANK_MODEL_ID="${RERANK_MODEL_ID:-BAAI/bge-reranker-v2-m3}"
  RERANK_MODEL_DIR="${RERANK_MODEL_DIR:-BAAI--bge-reranker-v2-m3}"
}

download_hf_model() {
  local repo_id="$1"
  local dest_dir="$2"
  log "Downloading Hugging Face model '${repo_id}' -> ${dest_dir}"
  mkdir -p "${dest_dir}"

  if command -v hf >/dev/null 2>&1; then
    hf download "${repo_id}" --local-dir "${dest_dir}"
  elif command -v huggingface-cli >/dev/null 2>&1; then
    huggingface-cli download "${repo_id}" --local-dir "${dest_dir}" --local-dir-use-symlinks False
  elif command -v python3 >/dev/null 2>&1 && python3 -c "import huggingface_hub" 2>/dev/null; then
    python3 -c "
from huggingface_hub import snapshot_download
snapshot_download(repo_id='${repo_id}', local_dir='${dest_dir}', local_dir_use_symlinks=False)
"
  else
    err "No Hugging Face downloader found. Install 'huggingface_hub[cli]' (hf / huggingface-cli)."
  fi
}

main() {
  require_cmd sha256sum
  require_cmd df
  load_env
  check_disk_space "${ARTIFACTS_DIR}" "${MIN_FREE_GB}"

  local images_archive="${ARTIFACTS_DIR}/ark-images.tar.zst"
  local models_root="${ARTIFACTS_DIR}/models"
  local snapshots_dir="${ARTIFACTS_DIR}/snapshots"
  mkdir -p "${models_root}" "${snapshots_dir}"

  if [[ "${SKIP_IMAGES}" == false ]]; then
    require_cmd docker
    require_cmd zstd

    local pull_images=(
      "${VLLM_IMAGE}"
      "${TEI_IMAGE}"
      "${QDRANT_IMAGE}"
      "${OPEN_WEBUI_IMAGE}"
      "${POSTGRES_IMAGE}"
      "${TIKA_IMAGE}"
      "${CADDY_IMAGE}"
      "${KEYCLOAK_IMAGE}"
      "${OAUTH2_PROXY_IMAGE}"
    )

    for img in "${pull_images[@]}"; do
      if [[ "${img}" == "${TIKA_IMAGE}" && -n "${TIKA_IMAGE_DIGEST}" && "${img}" != *@sha256:* ]]; then
        local pinned_ref="${img}@${TIKA_IMAGE_DIGEST}"
        log "Pulling Tika image with pinned digest: ${pinned_ref}"
        docker pull "${pinned_ref}"
        docker tag "${pinned_ref}" "${img}"
      else
        log "Pulling Docker image: ${img}"
        docker pull "${img}"
      fi
    done

    log "Building local ark-admin image: ${ARK_ADMIN_IMAGE} (ARK v${ARK_VERSION})"
    local git_commit build_date
    git_commit="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
    build_date="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    docker build \
      --build-arg "ARK_VERSION=${ARK_VERSION}" \
      --build-arg "ARK_GIT_COMMIT=${git_commit}" \
      --build-arg "ARK_BUILD_DATE=${build_date}" \
      -t "${ARK_ADMIN_IMAGE}" "${REPO_ROOT}/admin"

    local images=("${pull_images[@]}" "${ARK_ADMIN_IMAGE}")

    log "Recording image provenance digests -> ${ARTIFACTS_DIR}/ark-images.digests.txt"
    : > "${ARTIFACTS_DIR}/ark-images.digests.txt"
    for img in "${images[@]}"; do
      docker image inspect --format '{{.Id}} {{json .RepoTags}} {{json .RepoDigests}}' "${img}" >> "${ARTIFACTS_DIR}/ark-images.digests.txt"
    done

    log "Saving and compressing Docker images to ${images_archive} (zstd -T0)..."
    docker save "${images[@]}" | zstd -T0 -3 -f -o "${images_archive}"

    log "Generating SHA-256 checksum for ${images_archive}..."
    (cd "${ARTIFACTS_DIR}" && sha256sum "$(basename "${images_archive}")" > "$(basename "${images_archive}").sha256")
    log "Image bundle ready: ${images_archive}"
  else
    log "Skipping Docker image pull/save (--skip-images)."
  fi

  if [[ "${SKIP_MODELS}" == false ]]; then
    download_hf_model "${LLM_MODEL_ID}" "${models_root}/${LLM_MODEL_DIR}"
    download_hf_model "${EMBED_MODEL_ID}" "${models_root}/${EMBED_MODEL_DIR}"
    download_hf_model "${RERANK_MODEL_ID}" "${models_root}/${RERANK_MODEL_DIR}"

    if [[ "${PACKAGE_MODELS}" == true ]]; then
      require_cmd tar
      require_cmd zstd
      local models_archive="${ARTIFACTS_DIR}/ark-models.tar.zst"
      log "Archiving models into ${models_archive}..."
      tar -C "${ARTIFACTS_DIR}" -cf - models | zstd -T0 -3 -f -o "${models_archive}"
      (cd "${ARTIFACTS_DIR}" && sha256sum "$(basename "${models_archive}")" > "$(basename "${models_archive}").sha256")
      log "Model archive ready: ${models_archive}"
    fi
  else
    log "Skipping Hugging Face model downloads (--skip-models)."
  fi

  local bundle_manifest="${ARTIFACTS_DIR}/ark-bundle.json"
  local git_commit
  git_commit="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
  python3 - "${bundle_manifest}" "${ARK_VERSION}" "${git_commit}" "${LLM_MODEL_ID}" "${LLM_MODEL_DIR}" "${EMBED_MODEL_ID}" "${EMBED_MODEL_DIR}" "${RERANK_MODEL_ID}" "${RERANK_MODEL_DIR}" <<'PYEOF'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

out_path = Path(sys.argv[1])
payload = {
    "product": "ARK (Air-gapped Retrieval & Knowledge)",
    "ark_version": sys.argv[2],
    "license": "Apache-2.0",
    "git_commit": sys.argv[3],
    "bundled_at": datetime.now(timezone.utc).isoformat(),
    "models": {
        "llm": {"id": sys.argv[4], "dir": sys.argv[5]},
        "embed": {"id": sys.argv[6], "dir": sys.argv[7]},
        "rerank": {"id": sys.argv[8], "dir": sys.argv[9]},
    },
}
out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PYEOF
  (cd "${ARTIFACTS_DIR}" && sha256sum "$(basename "${bundle_manifest}")" > "$(basename "${bundle_manifest}").sha256")
  log "Bundle metadata written: ${bundle_manifest}"

  log "Offline bundling complete (ARK v${ARK_VERSION}). Transfer ${ARTIFACTS_DIR} and repository files to the air-gapped host."
}

main "$@"
