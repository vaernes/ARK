"""
ARK Legacy Connector Compatibility Shim.

All extraction, chunking, and embedding primitives now live in `admin/ingest.py`,
and built-in connectors (`smb_folder`, `sharepoint`, `mfiles`, `http_json`) are
implemented as first-party plugins in `admin/builtin_plugins/`.
"""

from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient

from ingest import (
    SUPPORTED_EXTENSIONS,
    chunk_text,
    embed_texts,
    extract_text_with_tika,
    process_ingest_documents,
)
from plugins import discover_plugins, execute_plugin

__all__ = [
    "SUPPORTED_EXTENSIONS",
    "extract_text_with_tika",
    "chunk_text",
    "embed_texts",
    "CONNECTOR_RUNNERS",
]


def _run_plugin_compat(plugin_id: str):
    def _runner(
        *,
        config: dict[str, Any],
        qdrant: QdrantClient,
        collection_name: str,
        tika_url: str,
        tei_url: str,
        db_dsn: str = "",
    ) -> dict[str, Any]:
        plugins = discover_plugins()
        if plugin_id not in plugins:
            raise KeyError(f"Plugin '{plugin_id}' not found.")
        docs, _, _ = execute_plugin(plugins[plugin_id], config=config)
        if db_dsn:
            stats = process_ingest_documents(
                documents=docs,
                qdrant=qdrant,
                db_dsn=db_dsn,
                project_slug=collection_name,
                collection_name=collection_name,
                source_key=f"compat:{plugin_id}",
                source_system=plugin_id,
                tika_url=tika_url,
                tei_url=tei_url,
                embed_model="BAAI/bge-m3",
                embed_vector_size=1024,
            )
            return stats.to_dict()
        return {"files_processed": len(docs), "chunks_upserted": 0}

    return _runner


CONNECTOR_RUNNERS = {
    "smb_folder": _run_plugin_compat("smb_folder"),
    "sharepoint": _run_plugin_compat("sharepoint"),
    "mfiles": _run_plugin_compat("mfiles"),
}
