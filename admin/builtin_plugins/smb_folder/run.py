from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ark_ingest import Checkpoint, Document, IngestContext, run

DEFAULT_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".html"}
_FORBIDDEN_PREFIXES = (
    "/app",
    "/backups",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib64",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
    "/var/log",
    "/var/run",
)


def _validate_share_root(raw_path: str) -> Path:
    cleaned = str(raw_path or "").strip()
    if not cleaned:
        raise ValueError("SMB/Folder path cannot be empty.")
    raw_p = Path(cleaned)
    if ".." in raw_p.parts:
        raise ValueError("SMB/Folder path must not contain '..' segments.")
    resolved = raw_p.resolve()
    resolved_str = str(resolved)
    if resolved_str == "/" or any(resolved_str == p or resolved_str.startswith(p + "/") for p in _FORBIDDEN_PREFIXES):
        raise ValueError(f"SMB/Folder path '{resolved_str}' is forbidden.")
    raw_env = os.environ.get("ARK_ALLOWED_INGEST_PATHS", "").strip()
    if raw_env:
        allowed_roots = [Path(p.strip()).resolve() for p in raw_env.split(",") if p.strip()]
    else:
        allowed_roots = [Path("/shares").resolve(), Path(tempfile.gettempdir()).resolve()]
    if not any(resolved == r or r in resolved.parents for r in allowed_roots):
        raise ValueError(f"SMB/Folder path '{resolved_str}' is outside allowed ingest roots.")
    return resolved


def sync(ctx: IngestContext):
    root = _validate_share_root(ctx.config["path"])
    if not root.is_dir():
        raise FileNotFoundError(f"SMB/Folder path does not exist: {root}")

    raw_exts = (ctx.config.get("extensions") or "").strip()
    if raw_exts:
        exts = {("." + e.strip().lstrip(".")).lower() for e in raw_exts.split(",") if e.strip()}
    else:
        exts = DEFAULT_EXTENSIONS

    emitted = 0
    for file_path in sorted(root.rglob("*")):
        resolved_fp = file_path.resolve()
        if root not in resolved_fp.parents or not resolved_fp.is_file() or resolved_fp.suffix.lower() not in exts:
            continue
        rel_id = str(resolved_fp.relative_to(root))
        yield Document(
            doc_id=rel_id,
            filename=resolved_fp.name,
            content=resolved_fp.read_bytes(),
            content_type="binary",
            metadata={"relative_path": rel_id},
        )
        emitted += 1
    yield Checkpoint({"last_sync": ctx.now_iso(), "emitted": emitted})


if __name__ == "__main__":
    run(sync)
