#!/usr/bin/env bash
set -euo pipefail

# Backward-compatible wrapper preserved for existing runbooks/automation.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/sync-project-kb.sh" "$@"
