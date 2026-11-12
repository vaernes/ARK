"""Version and license metadata helpers for ARK (CalVer + Apache-2.0)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

DEFAULT_VERSION = "2026.11.0"
ARK_LICENSE = "Apache-2.0"
VERSION_SCHEME = "CalVer"


def get_ark_version() -> str:
    """Resolve the ARK release version from environment, local VERSION file, or default."""
    env_ver = (os.environ.get("ARK_VERSION") or "").strip()
    if env_ver and env_ver.lower() != "unknown":
        return env_ver

    base_dir = Path(__file__).resolve().parent
    for candidate in (base_dir / "VERSION", base_dir.parent / "VERSION"):
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8").strip()
            if text:
                return text

    return DEFAULT_VERSION


def get_version_info() -> dict[str, Any]:
    """Return structured ARK version, build, and license metadata."""
    return {
        "name": "ARK",
        "product": "ARK (Air-gapped Retrieval & Knowledge)",
        "version": get_ark_version(),
        "scheme": VERSION_SCHEME,
        "license": ARK_LICENSE,
        "git_commit": (os.environ.get("ARK_GIT_COMMIT") or "unknown").strip() or "unknown",
        "build_date": (os.environ.get("ARK_BUILD_DATE") or "unknown").strip() or "unknown",
        "air_gap_mode": True,
    }
