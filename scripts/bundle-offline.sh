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
# shellcheck source=load-env.sh
source "${SCRIPT_DIR}/load-env.sh"

ENV_FILE="${REPO_ROOT}/.env"
ARTIFACTS_DIR="${REPO_ROOT}/artifacts"
MIN_FREE_GB=100
SKIP_IMAGES=false
SKIP_MODELS=false
PACKAGE_MODELS=false
SIGNING_KEY=""
ALLOW_UNSIGNED=false

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
      --signing-key PATH  Ed25519 private key used to sign the bundle manifest
      --allow-unsigned    Explicitly create an unsigned development bundle
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
    --signing-key)       SIGNING_KEY="$2"; shift 2 ;;
    --allow-unsigned)    ALLOW_UNSIGNED=true; shift ;;
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

  if [[ -f "${REPO_ROOT}/versions.env" ]]; then
    load_ark_env "${REPO_ROOT}/versions.env" || err "Could not load versions.env."
  fi

  load_ark_env "${source_env}" || err "Could not load environment configuration."

  PYTHON_BASE_IMAGE="${PYTHON_BASE_IMAGE:-python:3.12-slim-bookworm@sha256:0f086103b026048cc50cb5da25ec435c023c5241a83fc44daea593377abc61c4}"
  VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.33.0}"
  VLLM_IMAGE_DIGEST="${VLLM_IMAGE_DIGEST:-sha256:91ee972ab751fb7180bc71c175865e36b091686600dca783d52b2511399810b3}"
  TEI_IMAGE="${TEI_IMAGE:-ghcr.io/huggingface/text-embeddings-inference:1.9.4}"
  TEI_IMAGE_DIGEST="${TEI_IMAGE_DIGEST:-sha256:4b722a72ca0b7e985d496666b51cf8b557850f57011dba040dd1aa982c42434a}"
  QDRANT_IMAGE="${QDRANT_IMAGE:-qdrant/qdrant:v1.17.1}"
  QDRANT_IMAGE_DIGEST="${QDRANT_IMAGE_DIGEST:-sha256:cd3e42737c684ee516ae5533218be93fd5288f41d0a466ed18dbdc22ef52a000}"
  OPEN_WEBUI_IMAGE="${OPEN_WEBUI_IMAGE:-ghcr.io/open-webui/open-webui:v0.13.3}"
  OPEN_WEBUI_IMAGE_DIGEST="${OPEN_WEBUI_IMAGE_DIGEST:-sha256:e731360d9e6eeb25482d90cf3d564ce81a1c4a10e6a84ed9e6c63aa7ac48b0b6}"
  POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:18-alpine}"
  POSTGRES_IMAGE_DIGEST="${POSTGRES_IMAGE_DIGEST:-sha256:fa1b61f5a4844f4d4b73c9b15716cfc75fe70c931f2d32723630f3b900056366}"
  TIKA_IMAGE="${TIKA_IMAGE:-apache/tika:3.3.0.0-full}"
  TIKA_IMAGE_DIGEST="${TIKA_IMAGE_DIGEST:-sha256:ecdd37e204308266b1f8e28569f8516ebd5b96d7a4518a71d3904710b20c03b3}"
  CADDY_IMAGE="${CADDY_IMAGE:-caddy:2.11.9-alpine}"
  CADDY_IMAGE_DIGEST="${CADDY_IMAGE_DIGEST:-sha256:fc9cbc100585871d18d4e451cca7021785c90a7006087427ae5ceb3e4efe6424}"
  KEYCLOAK_IMAGE="${KEYCLOAK_IMAGE:-quay.io/keycloak/keycloak:26.8.2}"
  KEYCLOAK_IMAGE_DIGEST="${KEYCLOAK_IMAGE_DIGEST:-sha256:4da21f9a53bcbac1026a0d654b740a81e98e5de4c45457076acc74b4d94b1551}"
  OAUTH2_PROXY_IMAGE="${OAUTH2_PROXY_IMAGE:-quay.io/oauth2-proxy/oauth2-proxy:v7.15.6}"
  OAUTH2_PROXY_IMAGE_DIGEST="${OAUTH2_PROXY_IMAGE_DIGEST:-sha256:01da265f8e371e6d3043b97829e171f3e4d53f9dc3817be15fb4fc29c92fe2a6}"
  ARK_ADMIN_IMAGE="${ARK_ADMIN_IMAGE:-ark-admin:latest}"
  ARK_LLM_ADMIN_IMAGE="${ARK_LLM_ADMIN_IMAGE:-ark-llm-admin:latest}"

  LLM_MODEL_ID="${LLM_MODEL_ID:-Qwen/Qwen2.5-14B-Instruct}"
  LLM_MODEL_REVISION="${LLM_MODEL_REVISION:-cf98f3b3bbb457ad9e2bb7baf9a0125b6b88caa8}"
  LLM_MODEL_DIR="${LLM_MODEL_DIR:-Qwen--Qwen2.5-14B-Instruct}"
  EMBED_MODEL_ID="${EMBED_MODEL_ID:-BAAI/bge-m3}"
  EMBED_MODEL_REVISION="${EMBED_MODEL_REVISION:-5617a9f61b028005a4858fdac845db406aefb181}"
  EMBED_MODEL_DIR="${EMBED_MODEL_DIR:-BAAI--bge-m3}"
  RERANK_MODEL_ID="${RERANK_MODEL_ID:-BAAI/bge-reranker-v2-m3}"
  RERANK_MODEL_REVISION="${RERANK_MODEL_REVISION:-953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e}"
  RERANK_MODEL_DIR="${RERANK_MODEL_DIR:-BAAI--bge-reranker-v2-m3}"
}

download_hf_model() {
  local repo_id="$1"
  local dest_dir="$2"
  local revision="$3"
  [[ "${revision}" =~ ^[0-9a-f]{40}$ ]] || err "Model '${repo_id}' requires a 40-character immutable revision."
  log "Downloading Hugging Face model '${repo_id}' at ${revision} -> ${dest_dir}"
  mkdir -p "${dest_dir}"

  if command -v hf >/dev/null 2>&1; then
    hf download "${repo_id}" --revision "${revision}" --local-dir "${dest_dir}"
  elif command -v huggingface-cli >/dev/null 2>&1; then
    huggingface-cli download "${repo_id}" --revision "${revision}" --local-dir "${dest_dir}" --local-dir-use-symlinks False
  elif command -v python3 >/dev/null 2>&1 && python3 -c "import huggingface_hub" 2>/dev/null; then
    python3 - "${repo_id}" "${dest_dir}" "${revision}" <<'PYEOF'
import sys
from huggingface_hub import snapshot_download
snapshot_download(repo_id=sys.argv[1], local_dir=sys.argv[2], revision=sys.argv[3])
PYEOF
  else
    err "No Hugging Face downloader found. Install 'huggingface_hub[cli]' (hf / huggingface-cli)."
  fi
}

main() {
  require_cmd sha256sum
  require_cmd df
  require_cmd python3
  if [[ -z "${SIGNING_KEY}" && "${ALLOW_UNSIGNED}" != true ]]; then
    err "Provide --signing-key or explicitly select --allow-unsigned for development."
  fi
  if [[ -n "${SIGNING_KEY}" && "${ALLOW_UNSIGNED}" == true ]]; then
    err "--signing-key and --allow-unsigned cannot be combined."
  fi
  if [[ -n "${SIGNING_KEY}" ]]; then
    require_cmd openssl
    [[ -f "${SIGNING_KEY}" ]] || err "Signing key not found: ${SIGNING_KEY}"
    if [[ "${SKIP_MODELS}" == false && "${PACKAGE_MODELS}" == false ]]; then
      err "Signed bundles containing models require --package-models."
    fi
  fi
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

    local pull_digests=(
      "${VLLM_IMAGE_DIGEST}" "${TEI_IMAGE_DIGEST}" "${QDRANT_IMAGE_DIGEST}"
      "${OPEN_WEBUI_IMAGE_DIGEST}" "${POSTGRES_IMAGE_DIGEST}" "${TIKA_IMAGE_DIGEST}"
      "${CADDY_IMAGE_DIGEST}" "${KEYCLOAK_IMAGE_DIGEST}" "${OAUTH2_PROXY_IMAGE_DIGEST}"
    )
    local index img digest pinned_ref
    for index in "${!pull_images[@]}"; do
      img="${pull_images[$index]}"
      digest="${pull_digests[$index]}"
      [[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]] || err "Invalid digest for image ${img}."
      if [[ "${img}" == *@sha256:* ]]; then
        [[ "${img##*@}" == "${digest}" ]] || err "Image and configured digest differ for ${img}."
        pinned_ref="${img}"
      else
        pinned_ref="${img}@${digest}"
      fi
      log "Pulling Docker image with pinned digest: ${pinned_ref}"
      docker pull "${pinned_ref}"
      if [[ "${img}" != *@sha256:* ]]; then
        docker tag "${pinned_ref}" "${img}"
      fi
    done

    log "Building local ark-admin image: ${ARK_ADMIN_IMAGE} (ARK v${ARK_VERSION})"
    local git_commit build_date
    git_commit="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
    build_date="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    local build_cmd=(docker build)
    if docker buildx version >/dev/null 2>&1; then
      build_cmd=(docker buildx build --load)
    fi
    "${build_cmd[@]}" \
      --build-arg "PYTHON_BASE_IMAGE=${PYTHON_BASE_IMAGE}" \
      --build-arg "ARK_VERSION=${ARK_VERSION}" \
      --build-arg "ARK_GIT_COMMIT=${git_commit}" \
      --build-arg "ARK_BUILD_DATE=${build_date}" \
      -t "${ARK_ADMIN_IMAGE}" "${REPO_ROOT}/admin"

    log "Building local ark-llm-admin image: ${ARK_LLM_ADMIN_IMAGE} (ARK v${ARK_VERSION})"
    "${build_cmd[@]}" \
      --build-arg "PYTHON_BASE_IMAGE=${PYTHON_BASE_IMAGE}" \
      --build-arg "ARK_VERSION=${ARK_VERSION}" \
      --build-arg "ARK_GIT_COMMIT=${git_commit}" \
      --build-arg "ARK_BUILD_DATE=${build_date}" \
      -t "${ARK_LLM_ADMIN_IMAGE}" "${REPO_ROOT}/llm-admin"

    local images=("${pull_images[@]}" "${ARK_ADMIN_IMAGE}" "${ARK_LLM_ADMIN_IMAGE}")

    log "Recording image provenance digests -> ${ARTIFACTS_DIR}/ark-images.digests.txt"
    : > "${ARTIFACTS_DIR}/ark-images.digests.txt"
    for img in "${images[@]}"; do
      docker image inspect --format '{{.Id}} {{json .RepoTags}} {{json .RepoDigests}}' "${img}" >> "${ARTIFACTS_DIR}/ark-images.digests.txt"
    done
    log "Recording built-image operating system and Python dependencies..."
    : > "${ARTIFACTS_DIR}/ark-dependencies.txt"
    for img in "${ARK_ADMIN_IMAGE}" "${ARK_LLM_ADMIN_IMAGE}"; do
      printf 'IMAGE %s\n' "${img}" >> "${ARTIFACTS_DIR}/ark-dependencies.txt"
      docker run --rm --network none "${img}" sh -c \
        'dpkg-query -W; python3 -m pip freeze' \
        >> "${ARTIFACTS_DIR}/ark-dependencies.txt"
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
    download_hf_model "${LLM_MODEL_ID}" "${models_root}/${LLM_MODEL_DIR}" "${LLM_MODEL_REVISION}"
    download_hf_model "${EMBED_MODEL_ID}" "${models_root}/${EMBED_MODEL_DIR}" "${EMBED_MODEL_REVISION}"
    download_hf_model "${RERANK_MODEL_ID}" "${models_root}/${RERANK_MODEL_DIR}" "${RERANK_MODEL_REVISION}"

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
  python3 - "${bundle_manifest}" "${ARK_VERSION}" "${git_commit}" "${LLM_MODEL_ID}" "${LLM_MODEL_DIR}" "${EMBED_MODEL_ID}" "${EMBED_MODEL_DIR}" "${RERANK_MODEL_ID}" "${RERANK_MODEL_DIR}" "${LLM_MODEL_REVISION}" "${EMBED_MODEL_REVISION}" "${RERANK_MODEL_REVISION}" <<'PYEOF'
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
        "llm": {"id": sys.argv[4], "dir": sys.argv[5], "revision": sys.argv[10]},
        "embed": {"id": sys.argv[6], "dir": sys.argv[7], "revision": sys.argv[11]},
        "rerank": {"id": sys.argv[8], "dir": sys.argv[9], "revision": sys.argv[12]},
    },
}
out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PYEOF
  if [[ -n "${SIGNING_KEY}" ]]; then
    local -a sign_args=(sign --manifest "${bundle_manifest}" --key "${SIGNING_KEY}")
    if [[ "${SKIP_IMAGES}" == false ]]; then
      sign_args+=(--artifact "images=${images_archive}" --artifact "image_provenance=${ARTIFACTS_DIR}/ark-images.digests.txt"
                  --artifact "dependencies=${ARTIFACTS_DIR}/ark-dependencies.txt")
    fi
    if [[ "${SKIP_MODELS}" == false && "${PACKAGE_MODELS}" == true ]]; then
      sign_args+=(--artifact "models=${ARTIFACTS_DIR}/ark-models.tar.zst")
    fi
    python3 "${SCRIPT_DIR}/bundle_manifest.py" "${sign_args[@]}"
  else
    warn "Unsigned bundle created; do not use it as an authenticated production bundle."
  fi
  (cd "${ARTIFACTS_DIR}" && sha256sum "$(basename "${bundle_manifest}")" > "$(basename "${bundle_manifest}").sha256")
  log "Bundle metadata written: ${bundle_manifest}"

  log "Offline bundling complete (ARK v${ARK_VERSION}). Transfer ${ARTIFACTS_DIR} and repository files to the air-gapped host."
}

main "$@"
