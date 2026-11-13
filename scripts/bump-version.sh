#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# ARK CalVer Release Version Bump Helper
# Updates VERSION, README.md, admin/version.py, admin/Dockerfile,
# llm-admin/Dockerfile, llm-admin/app.py, docker-compose.yml,
# docker-compose.llm.yml, .env.example (and optionally CHANGELOG.md) atomically
# so all build defaults and documentation stay synchronized.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--update-changelog] [<YYYY.MM.PATCH[-prerelease]> | --bump <patch|calver-month>]

Examples:
  $(basename "$0") 2026.11.1
  $(basename "$0") --update-changelog 2026.11.1
  $(basename "$0") --update-changelog --bump patch
  $(basename "$0") --update-changelog --bump calver-month
  $(basename "$0") 2026.12.0-rc.1
EOF
}

UPDATE_CHANGELOG=false
BUMP_KIND=""
TARGET_VERSION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --update-changelog)
      UPDATE_CHANGELOG=true
      shift
      ;;
    --bump)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --bump requires 'patch' or 'calver-month'." >&2
        exit 1
      fi
      BUMP_KIND="$2"
      shift 2
      ;;
    -*)
      echo "ERROR: Unknown option '$1'." >&2
      usage >&2
      exit 1
      ;;
    *)
      if [[ -n "${TARGET_VERSION}" ]]; then
        echo "ERROR: Unexpected extra argument '$1'." >&2
        usage >&2
        exit 1
      fi
      TARGET_VERSION="$1"
      shift
      ;;
  esac
done

if [[ -n "${TARGET_VERSION}" && -n "${BUMP_KIND}" ]]; then
  echo "ERROR: Specify either an explicit version or --bump, not both." >&2
  exit 1
fi

if [[ -z "${TARGET_VERSION}" && -z "${BUMP_KIND}" ]]; then
  usage >&2
  exit 1
fi

if [[ -n "${BUMP_KIND}" && "${BUMP_KIND}" != "patch" && "${BUMP_KIND}" != "calver-month" ]]; then
  echo "ERROR: Invalid --bump mode '${BUMP_KIND}'. Expected 'patch' or 'calver-month'." >&2
  exit 1
fi

python3 - "${REPO_ROOT}" "${TARGET_VERSION}" "${BUMP_KIND}" "${UPDATE_CHANGELOG}" <<'PYEOF'
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
target_arg = sys.argv[2].strip()
bump_kind = sys.argv[3].strip()
update_changelog = sys.argv[4].lower() == "true"

calver_re = re.compile(r"^(\d{4})\.(0[1-9]|1[0-2])\.(\d+)(?:-([0-9A-Za-z.-]+))?$")

old_ver = (root / "VERSION").read_text(encoding="utf-8").strip()
old_match = calver_re.match(old_ver)
if not old_match:
    raise SystemExit(f"ERROR: Existing VERSION '{old_ver}' is not valid CalVer.")

if target_arg:
    if not calver_re.match(target_arg):
        raise SystemExit(
            f"ERROR: Invalid CalVer version '{target_arg}'. Expected YYYY.MM.PATCH (e.g. 2026.11.1)."
        )
    new_ver = target_arg
else:
    cur_y = int(old_match.group(1))
    cur_m = int(old_match.group(2))
    cur_p = int(old_match.group(3))
    cur_pre = old_match.group(4)

    now = datetime.now(timezone.utc)
    now_y, now_m = now.year, now.month

    if bump_kind == "patch":
        if (now_y, now_m) > (cur_y, cur_m):
            new_ver = f"{now_y}.{now_m:02d}.0"
        elif cur_pre:
            new_ver = f"{cur_y}.{cur_m:02d}.{cur_p}"
        else:
            new_ver = f"{cur_y}.{cur_m:02d}.{cur_p + 1}"
    else:  # calver-month
        if (now_y, now_m) > (cur_y, cur_m):
            new_ver = f"{now_y}.{now_m:02d}.0"
        else:
            next_m = cur_m + 1
            next_y = cur_y + (1 if next_m > 12 else 0)
            if next_m > 12:
                next_m = 1
            new_ver = f"{next_y}.{next_m:02d}.0"

(root / "VERSION").write_text(f"{new_ver}\n", encoding="utf-8")


def replace_exact(rel: str, pattern: str, repl: str) -> None:
    p = root / rel
    text = p.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, repl, text, flags=re.MULTILINE)
    if count == 0:
        raise RuntimeError(f"Pattern not found in {rel}: {pattern}")
    p.write_text(updated, encoding="utf-8")


replace_exact("README.md", r'^(\*\*Version:\*\* )`[^`]+`', rf'\g<1>`{new_ver}`')
replace_exact("admin/version.py", r'^DEFAULT_VERSION = ".*"$', f'DEFAULT_VERSION = "{new_ver}"')
replace_exact("admin/Dockerfile", r'^ARG ARK_VERSION=.*$', f'ARG ARK_VERSION={new_ver}')
replace_exact("llm-admin/Dockerfile", r'^ARG ARK_VERSION=.*$', f'ARG ARK_VERSION={new_ver}')
replace_exact(
    "llm-admin/app.py",
    r'^ARK_VERSION = os\.environ\.get\("ARK_VERSION", ".*"\)$',
    f'ARK_VERSION = os.environ.get("ARK_VERSION", "{new_ver}")',
)
replace_exact("docker-compose.yml", r'(\$\{ARK_VERSION:-)[^}]+(\})', rf'\g<1>{new_ver}\g<2>')
replace_exact("docker-compose.llm.yml", r'(\$\{ARK_VERSION:-)[^}]+(\})', rf'\g<1>{new_ver}\g<2>')
replace_exact(".env.example", r'^ARK_VERSION=.*$', f'ARK_VERSION={new_ver}')

if update_changelog:
    changelog_path = root / "CHANGELOG.md"
    changelog = changelog_path.read_text(encoding="utf-8")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not re.search(rf"^## \[{re.escape(new_ver)}\]", changelog, flags=re.M):
        unreleased_re = re.compile(
            r"^(## \[Unreleased\]\s*\n)(.*?)(?=^## \[|\Z)",
            flags=re.M | re.S,
        )
        m = unreleased_re.search(changelog)
        if not m:
            raise RuntimeError("CHANGELOG.md is missing '## [Unreleased]' section.")
        unreleased_body = m.group(2).strip()
        if not unreleased_body:
            unreleased_body = f"### Changed\n- Release `v{new_ver}`."
        replacement = f"## [Unreleased]\n\n## [{new_ver}] - {today}\n\n{unreleased_body}\n\n"
        changelog = changelog[: m.start()] + replacement + changelog[m.end() :]

    changelog = re.sub(
        r"^(\[Unreleased\]: https://github\.com/vaernes/ARK/compare/)v\S+?(\.\.\.HEAD)$",
        rf"\g<1>v{new_ver}\g<2>",
        changelog,
        flags=re.M,
    )
    if not re.search(rf"^\[{re.escape(new_ver)}\]:", changelog, flags=re.M):
        changelog, count = re.subn(
            r"^(\[Unreleased\]: [^\n]+\n)",
            rf"\g<1>[{new_ver}]: https://github.com/vaernes/ARK/compare/v{old_ver}...v{new_ver}\n",
            changelog,
            count=1,
            flags=re.M,
        )
        if count == 0:
            changelog = (
                changelog.rstrip()
                + f"\n[{new_ver}]: https://github.com/vaernes/ARK/compare/v{old_ver}...v{new_ver}\n"
            )
    changelog_path.write_text(changelog, encoding="utf-8")

print(f"Bumped ARK version: {old_ver} -> {new_ver}")
PYEOF

NEW_VERSION="$(tr -d '[:space:]' < "${REPO_ROOT}/VERSION")"
echo "Next steps:"
if [[ "${UPDATE_CHANGELOG}" != "true" ]]; then
  echo "  1. Update CHANGELOG.md with a '## [${NEW_VERSION}] - $(date -u +%Y-%m-%d)' section."
  echo "  2. Run 'python3 -m unittest discover -s tests -v' and './scripts/test-integration.sh'."
  echo "  3. Commit changes and create tag: git tag -a v${NEW_VERSION} -m 'Release v${NEW_VERSION}'"
else
  echo "  1. Review CHANGELOG.md release notes for '${NEW_VERSION}'."
  echo "  2. Run 'python3 -m unittest discover -s tests -v' and './scripts/test-integration.sh'."
  echo "  3. Commit changes and create tag: git tag -a v${NEW_VERSION} -m 'Release v${NEW_VERSION}'"
fi
