"""
ARK Ingest SDK (`ark_ingest`) for Custom Ingest Scripts & Connector Plugins.

Provides a lightweight, stdlib-only contract for reading connector config/cursor
from stdin and streaming normalized ARK Document IR JSONL records to stdout.

Example script (`/ingest-scripts/my_source.py`):

    from ark_ingest import Document, Checkpoint, run

    def sync(ctx):
        for row in ...:
            yield Document(
                doc_id=f"erp/{row['id']}",
                filename=f"{row['id']}.md",
                content=row["body"],
                metadata={"dept": row["dept"]},
            )
        yield Checkpoint({"last_id": row["id"]})

    if __name__ == "__main__":
        run(sync)
"""

from __future__ import annotations

import base64
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable


@dataclass
class Document:
    doc_id: str
    content: str | bytes = ""
    filename: str = ""
    content_type: str = "text"  # "text" | "binary" | "chunks"
    action: str = "upsert"  # "upsert" | "delete"
    metadata: dict[str, Any] = field(default_factory=dict)
    chunking: dict[str, Any] | None = None
    chunks: list[dict[str, Any]] | None = None
    content_hash: str = ""

    def to_ir(self) -> dict[str, Any]:
        ctype = self.content_type
        encoded_content: str
        if isinstance(self.content, (bytes, bytearray)):
            ctype = "binary"
            encoded_content = base64.b64encode(bytes(self.content)).decode("ascii")
        else:
            encoded_content = str(self.content or "")

        record: dict[str, Any] = {
            "type": "document",
            "doc_id": self.doc_id,
            "action": self.action,
            "content_type": ctype,
            "filename": self.filename or self.doc_id,
            "content": encoded_content,
            "metadata": self.metadata or {},
        }
        if self.content_hash:
            record["content_hash"] = self.content_hash
        if self.chunking:
            record["chunking"] = self.chunking
        if self.chunks is not None:
            record["content_type"] = "chunks"
            record["chunks"] = self.chunks
        return record


@dataclass
class Checkpoint:
    cursor: dict[str, Any]

    def to_ir(self) -> dict[str, Any]:
        return {"type": "checkpoint", "cursor": self.cursor}


@dataclass
class Log:
    message: str
    level: str = "info"

    def to_ir(self) -> dict[str, Any]:
        return {"type": "log", "level": self.level, "message": self.message}


class IngestContext:
    """Execution context passed to `sync(ctx)`."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.config: dict[str, Any] = payload.get("config") or {}
        self.cursor: dict[str, Any] = payload.get("cursor") or {}
        self.dry_run: bool = bool(payload.get("dry_run", False))
        self.limit: int | None = payload.get("limit")

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()


def run(sync_fn: Callable[[IngestContext], Iterable[Document | Checkpoint | Log | dict[str, Any]]]) -> None:
    """Read input JSON from stdin, execute `sync_fn(ctx)`, and emit NDJSON IR lines."""
    raw = sys.stdin.read().strip()
    payload = json.loads(raw) if raw else {}
    ctx = IngestContext(payload)

    emitted_docs = 0
    for item in sync_fn(ctx):
        if isinstance(item, (Document, Checkpoint, Log)):
            record = item.to_ir()
        elif isinstance(item, dict):
            record = item
        else:
            raise TypeError(f"Unsupported yielded item type: {type(item).__name__}")

        if record.get("type") == "document":
            emitted_docs += 1
            if ctx.limit is not None and emitted_docs > ctx.limit:
                break

        sys.stdout.write(json.dumps(record, default=str) + "\n")
        sys.stdout.flush()
