#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# ARK CalVer Release Version Bump Helper
# Updates VERSION, admin/version.py, admin/Dockerfile, docker-compose.yml,
# and .env.example atomically so all build defaults stay synchronized.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
  cat <<EOF
Usage: $(basename "$0") <YYYY.MM.PATCH[-prerelease]>

Example:
  $(basename "$0") 2026.11.1
  $(basename "$0") 2026.12.0-rc.1
EOF
}

if [[ $# -ne 1 ]] || [[ "${1}" == "-h" ]] || [[ "${1}" == "--help" ]]; then
  usage
  exit $([[ $# -eq 1 ]] && echo 0 || echo 1)
fi

NEW_VERSION="$1"
if [[ ! "${NEW_VERSION}" =~ ^[0-9]{4}\.(0[1-9]|1[0-2])\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]]; then
  echo "ERROR: Invalid CalVer version '${NEW_VERSION}'. Expected YYYY.MM.PATCH (e.g. 2026.11.1)." >&2
  exit 1
fi

OLD_VERSION="$(tr -d '[:space:]' < "${REPO_ROOT}/VERSION")"

python3 - "${REPO_ROOT}" "${OLD_VERSION}" "${NEW_VERSION}" <<'PYEOF'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
old_ver = sys.argv[2]
new_ver = sys.argv[3]

(root / "VERSION").write_text(f"{new_ver}\n", encoding="utf-8")

def replace_exact(rel: str, pattern: str, repl: str) -> None:
    p = root / rel
    text = p.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, repl, text, flags=re.MULTILINE)
    if count == 0:
        raise RuntimeError(f"Pattern not found in {rel}: {pattern}")
    p.write_text(updated, encoding="utf-8")

replace_exact("admin/version.py", r'^DEFAULT_VERSION = ".*"$', f'DEFAULT_VERSION = "{new_ver}"')
replace_exact("admin/Dockerfile", r'^ARG ARK_VERSION=.*$', f'ARG ARK_VERSION={new_ver}')
replace_exact("docker-compose.yml", r'(\$\{ARK_VERSION:-)[^}]+(\})', rf'\g<1>{new_ver}\g<2>')
replace_exact(".env.example", r'^ARK_VERSION=.*$', f'ARK_VERSION={new_ver}')

print(f"Bumped ARK version: {old_ver} -> {new_ver}")
PYEOF

echo "Next steps:"
echo "  1. Update CHANGELOG.md with a '## [${NEW_VERSION}] - $(date -u +%Y-%m-%d)' section."
echo "  2. Run 'python3 -m unittest discover -s tests -v' and './scripts/test-integration.sh'."
echo "  3. Commit changes and create tag: git tag -a v${NEW_VERSION} -m 'Release v${NEW_VERSION}'"
