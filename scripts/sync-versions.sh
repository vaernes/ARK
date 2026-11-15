#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# ARK Product Version Sync Helper
# Reads ./versions.env (single source of truth) and synchronizes:
#   - .env.example
#   - docker-compose.yml
#   - docker-compose.llm.yml
#   - scripts/bundle-offline.sh
#   - README.md (Bundled Products & Versions table)
#
# Usage:
#   ./scripts/sync-versions.sh          # Apply versions.env across repo
#   ./scripts/sync-versions.sh --check  # Verify all files match versions.env
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CHECK_ONLY=false

if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=true
elif [[ -n "${1:-}" ]]; then
  echo "Usage: $(basename "$0") [--check]" >&2
  exit 1
fi

python3 - "${REPO_ROOT}" "${CHECK_ONLY}" <<'PYEOF'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
check_only = sys.argv[2].lower() == "true"

versions_path = root / "versions.env"
if not versions_path.is_file():
    raise SystemExit(f"ERROR: Missing {versions_path}")

versions: dict[str, str] = {}
for raw_line in versions_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    if "=" not in line:
        raise SystemExit(f"ERROR: Invalid line in versions.env: {raw_line}")
    key, val = line.split("=", 1)
    versions[key.strip()] = val.strip()

required_keys = [
    "VLLM_IMAGE",
    "TEI_IMAGE",
    "QDRANT_IMAGE",
    "OPEN_WEBUI_IMAGE",
    "POSTGRES_IMAGE",
    "TIKA_IMAGE",
    "TIKA_IMAGE_DIGEST",
    "CADDY_IMAGE",
    "KEYCLOAK_IMAGE",
    "OAUTH2_PROXY_IMAGE",
]
for k in required_keys:
    if not versions.get(k):
        raise SystemExit(f"ERROR: Missing required key '{k}' in versions.env")


def split_image_tag(image_ref: str) -> tuple[str, str]:
    if ":" in image_ref.rsplit("/", 1)[-1]:
        repo, tag = image_ref.rsplit(":", 1)
        return repo, tag
    return image_ref, "latest"


def render_readme_block(v: dict[str, str]) -> str:
    rows = [
        ("vLLM", "VLLM_IMAGE", "Local OpenAI-compatible LLM inference engine"),
        ("Hugging Face TEI", "TEI_IMAGE", "Embeddings (`BAAI/bge-m3`) & Reranking (`BAAI/bge-reranker-v2-m3`)"),
        ("Qdrant", "QDRANT_IMAGE", "Isolated per-project vector database"),
        ("Open WebUI", "OPEN_WEBUI_IMAGE", "Optional shared WebChat UI (`/`), SSO-integrated"),
        ("PostgreSQL", "POSTGRES_IMAGE", "Relational store for `ark-admin`, Keycloak, and Open WebUI"),
        ("Apache Tika", "TIKA_IMAGE", "Offline document parsing & OCR extraction"),
        ("Caddy", "CADDY_IMAGE", "Local HTTPS reverse proxy & internal TLS termination"),
        ("Keycloak", "KEYCLOAK_IMAGE", "Enterprise SSO / Active Directory / OIDC identity broker"),
        ("OAuth2-Proxy", "OAUTH2_PROXY_IMAGE", "Forward-auth session gateway for ARK portals & consoles"),
    ]
    lines = [
        "<!-- ARK_VERSIONS_START -->",
        "| Product | Image | Pinned Version | Role |",
        "| :--- | :--- | :--- | :--- |",
    ]
    for name, var_name, role in rows:
        repo, tag = split_image_tag(v[var_name])
        lines.append(f"| **{name}** | `{repo}` | `{tag}` | {role} |")
    lines.append("<!-- ARK_VERSIONS_END -->")
    return "\n".join(lines)


drifted: list[str] = []


def apply_update(path: Path, new_text: str) -> None:
    old_text = path.read_text(encoding="utf-8")
    if old_text == new_text:
        return
    if check_only:
        drifted.append(str(path.relative_to(root)))
    else:
        path.write_text(new_text, encoding="utf-8")
        print(f"Updated {path.relative_to(root)}")


# 1. .env.example
env_path = root / ".env.example"
env_text = env_path.read_text(encoding="utf-8")
for k, val in versions.items():
    env_text = re.sub(rf"(?m)^{re.escape(k)}=.*$", f"{k}={val}", env_text)
apply_update(env_path, env_text)

# 2. docker-compose.yml & docker-compose.llm.yml
for rel in ("docker-compose.yml", "docker-compose.llm.yml"):
    p = root / rel
    txt = p.read_text(encoding="utf-8")
    for k, val in versions.items():
        if k.endswith("_IMAGE"):
            txt = re.sub(rf"\$\{{{re.escape(k)}:-[^}}]+\}}", f"${{{k}:-{val}}}", txt)
    apply_update(p, txt)

# 3. scripts/bundle-offline.sh
bundle_path = root / "scripts" / "bundle-offline.sh"
bundle_text = bundle_path.read_text(encoding="utf-8")
for k, val in versions.items():
    bundle_text = re.sub(
        rf'(?m)^(\s*{re.escape(k)}="\$\{{{re.escape(k)}:-)[^}}]+(\}}")$',
        rf"\g<1>{val}\g<2>",
        bundle_text,
    )
apply_update(bundle_path, bundle_text)

# 4. admin/Dockerfile postgresql-client-<major>
pg_major_match = re.search(r":(\d+)", versions["POSTGRES_IMAGE"])
if pg_major_match:
    pg_major = pg_major_match.group(1)
    dockerfile_path = root / "admin" / "Dockerfile"
    dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
    dockerfile_text = re.sub(r"postgresql-client-\d+", f"postgresql-client-{pg_major}", dockerfile_text)
    apply_update(dockerfile_path, dockerfile_text)

# 5. README.md table
readme_path = root / "README.md"
readme_text = readme_path.read_text(encoding="utf-8")
block = render_readme_block(versions)
marker_re = re.compile(r"<!-- ARK_VERSIONS_START -->.*?<!-- ARK_VERSIONS_END -->", re.S)
if not marker_re.search(readme_text):
    raise SystemExit("ERROR: Missing ARK_VERSIONS_START/END markers in README.md")
readme_text = marker_re.sub(block, readme_text, count=1)
apply_update(readme_path, readme_text)

if check_only and drifted:
    raise SystemExit(
        "ERROR: Product versions out of sync with versions.env in: "
        + ", ".join(drifted)
        + ". Run ./scripts/sync-versions.sh"
    )
PYEOF
