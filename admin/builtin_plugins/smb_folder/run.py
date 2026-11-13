from __future__ import annotations

from pathlib import Path

from ark_ingest import Document, IngestContext, run

DEFAULT_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".html"}


def sync(ctx: IngestContext):
    root = Path(ctx.config["path"])
    if not root.is_dir():
        raise FileNotFoundError(f"SMB/Folder path does not exist: {root}")

    raw_exts = (ctx.config.get("extensions") or "").strip()
    if raw_exts:
        exts = {("." + e.strip().lstrip(".")).lower() for e in raw_exts.split(",") if e.strip()}
    else:
        exts = DEFAULT_EXTENSIONS

    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file() or file_path.suffix.lower() not in exts:
            continue
        rel_id = str(file_path.relative_to(root))
        yield Document(
            doc_id=rel_id,
            filename=file_path.name,
            content=file_path.read_bytes(),
            content_type="binary",
            metadata={"relative_path": rel_id},
        )


if __name__ == "__main__":
    run(sync)
