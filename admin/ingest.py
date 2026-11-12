"""
ARK Unified Document Ingestion Engine.

Normalizes documents from any entrypoint (Push API, Offline Connector Plugins,
Declarative Pipelines, or Custom Scripts) through a single air-gap-safe pipeline:
- Document IR validation & reserved payload key protection
- Content-hash deduplication & incremental skip (PostgreSQL `ingest_documents`)
- Pluggable chunking strategies (chars, markdown, lines, none)
- Optional Apache Tika extraction for binary payloads
- TEI embedding using configured `EMBED_MODEL_ID`
- Isolated Qdrant upsert with stale-chunk cleanup & full-mirror pruning
- At-rest encryption for connector/source secrets
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

import psycopg
import requests
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.http.models import FieldCondition, Filter, MatchValue, PayloadSchemaType, PointStruct

logger = logging.getLogger("ark-ingest")

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".html"}
RESERVED_PAYLOAD_KEYS = frozenset({"text", "source", "source_id", "doc_id", "filename", "chunk_index", "content_hash"})


class ChunkConfig(BaseModel):
    strategy: Literal["chars", "markdown", "lines", "none"] = "chars"
    chunk_size: int = Field(default=1200, ge=64, le=32000)
    overlap: int = Field(default=200, ge=0, le=8000)


class PreChunk(BaseModel):
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestDocument(BaseModel):
    doc_id: str = Field(min_length=1, max_length=512)
    action: Literal["upsert", "delete"] = "upsert"
    content_type: Literal["text", "binary", "chunks"] = "text"
    filename: str = ""
    content: str = ""
    raw_bytes: bytes | None = Field(default=None, exclude=True)
    chunks: list[PreChunk] = Field(default_factory=list)
    content_hash: str = ""
    chunking: ChunkConfig = Field(default_factory=ChunkConfig)
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass
class IngestStats:
    docs_seen: int = 0
    docs_upserted: int = 0
    docs_skipped: int = 0
    docs_deleted: int = 0
    chunks_upserted: int = 0
    errors: int = 0
    error_messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "docs_seen": self.docs_seen,
            "docs_upserted": self.docs_upserted,
            "docs_skipped": self.docs_skipped,
            "docs_deleted": self.docs_deleted,
            "chunks_upserted": self.chunks_upserted,
            "errors": self.errors,
            "error_messages": self.error_messages[:20],
            # Compatibility aliases for legacy connector UI/tests
            "files_processed": self.docs_upserted + self.docs_skipped,
        }


# ------------------------------------------------------------------------------
# Secret Encryption Helpers (at-rest protection in PostgreSQL)
# ------------------------------------------------------------------------------

_FALLBACK_KEY_WARNED = False
_SECRET_VALUE_PREFIX = "enc:v1:"


def _resolve_fernet(secret_seed: str | None = None) -> Fernet:
    global _FALLBACK_KEY_WARNED
    env_secrets_key = os.environ.get("ARK_SECRETS_KEY", "").strip()
    env_proxy_key = os.environ.get("ARK_PROXY_AUTH_SECRET", "").strip()
    raw = (secret_seed or env_secrets_key or env_proxy_key or "ark-default-dev-secrets-key").strip()
    if not secret_seed and not env_secrets_key and not _FALLBACK_KEY_WARNED:
        _FALLBACK_KEY_WARNED = True
        if env_proxy_key:
            logger.warning("ARK_SECRETS_KEY is unset; falling back to ARK_PROXY_AUTH_SECRET for secret encryption.")
        else:
            logger.warning("ARK_SECRETS_KEY is unset; using development fallback key for secret encryption.")
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret_value(value: str, secret_seed: str | None = None) -> str:
    """Encrypt an individual scalar secret string for at-rest storage."""
    if not value:
        return ""
    if value.startswith(_SECRET_VALUE_PREFIX):
        return value
    token = _resolve_fernet(secret_seed).encrypt(value.encode("utf-8")).decode("ascii")
    return f"{_SECRET_VALUE_PREFIX}{token}"


def decrypt_secret_value(token: str, secret_seed: str | None = None) -> str:
    """Decrypt an `enc:v1:` secret string; return plaintext legacy values unchanged."""
    if not token or not token.startswith(_SECRET_VALUE_PREFIX):
        return token
    raw_tok = token[len(_SECRET_VALUE_PREFIX):].encode("ascii")
    try:
        return _resolve_fernet(secret_seed).decrypt(raw_tok).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        logger.warning("Failed to decrypt secret value: %s", exc)
        return ""


def encrypt_secrets(secrets_dict: dict[str, Any], secret_seed: str | None = None) -> bytes:
    if not secrets_dict:
        return b""
    payload = json.dumps(secrets_dict, separators=(",", ":")).encode("utf-8")
    return _resolve_fernet(secret_seed).encrypt(payload)


def decrypt_secrets(token: bytes | memoryview | None, secret_seed: str | None = None) -> dict[str, Any]:
    if not token:
        return {}
    raw_bytes = bytes(token)
    if not raw_bytes:
        return {}
    try:
        plain = _resolve_fernet(secret_seed).decrypt(raw_bytes)
        data = json.loads(plain.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (InvalidToken, ValueError, TypeError) as exc:
        logger.warning("Failed to decrypt source secrets: %s", exc)
        return {}


def split_config_and_secrets(
    config: dict[str, Any],
    secret_fields: Iterable[str],
    existing_secrets: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate public config from secret fields; preserve existing secret when masked/blank."""
    secret_set = set(secret_fields)
    public_cfg: dict[str, Any] = {}
    new_secrets: dict[str, Any] = dict(existing_secrets or {})
    for key, val in (config or {}).items():
        if key in secret_set:
            if val not in (None, "", "********"):
                new_secrets[key] = val
        else:
            public_cfg[key] = val
    return public_cfg, new_secrets


# ------------------------------------------------------------------------------
# Extraction, Chunking & Embedding
# ------------------------------------------------------------------------------

def extract_text_with_tika(tika_url: str, content: bytes, filename: str) -> str:
    resp = requests.put(
        f"{tika_url.rstrip('/')}/tika",
        data=content,
        headers={"Accept": "text/plain", "Content-Disposition": f'attachment; filename="{filename}"'},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.text.strip()


def chunk_text(text: str, chunk_size: int = 1200, overlap: int = 200) -> list[str]:
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    length = len(text)
    safe_overlap = min(overlap, max(chunk_size - 1, 0))
    while start < length:
        end = min(start + chunk_size, length)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= length:
            break
        start = max(end - safe_overlap, start + 1)
    return chunks


def chunk_markdown(text: str, chunk_size: int = 1200, overlap: int = 200) -> list[str]:
    if not text or not text.strip():
        return []
    sections = re.split(r"(?m)(?=^#{1,3}\s+)", text.strip())
    chunks: list[str] = []
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        if len(sec) <= chunk_size:
            chunks.append(sec)
        else:
            chunks.extend(chunk_text(sec, chunk_size=chunk_size, overlap=overlap))
    return chunks


def chunk_lines(text: str, chunk_size: int = 1200, overlap: int = 200) -> list[str]:
    if not text or not text.strip():
        return []
    lines = text.splitlines()
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for line in lines:
        add_len = len(line) + (1 if buf else 0)
        if buf and buf_len + add_len > chunk_size:
            chunks.append("\n".join(buf).strip())
            if overlap > 0:
                carry: list[str] = []
                carry_len = 0
                for prev in reversed(buf):
                    if carry_len + len(prev) + 1 > overlap:
                        break
                    carry.insert(0, prev)
                    carry_len += len(prev) + 1
                buf = carry
                buf_len = sum(len(x) + 1 for x in buf)
            else:
                buf = []
                buf_len = 0
        if len(line) > chunk_size:
            if buf:
                chunks.append("\n".join(buf).strip())
                buf = []
                buf_len = 0
            chunks.extend(chunk_text(line, chunk_size=chunk_size, overlap=overlap))
        else:
            buf.append(line)
            buf_len += add_len
    if buf:
        joined = "\n".join(buf).strip()
        if joined:
            chunks.append(joined)
    return [c for c in chunks if c]


def apply_chunking(text: str, cfg: ChunkConfig) -> list[str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    if cfg.strategy == "none":
        return [cleaned]
    if cfg.strategy == "markdown":
        return chunk_markdown(cleaned, chunk_size=cfg.chunk_size, overlap=cfg.overlap)
    if cfg.strategy == "lines":
        return chunk_lines(cleaned, chunk_size=cfg.chunk_size, overlap=cfg.overlap)
    return chunk_text(cleaned, chunk_size=cfg.chunk_size, overlap=cfg.overlap)


def embed_texts(tei_url: str, texts: list[str], model: str | None = None) -> list[list[float]]:
    if not texts:
        return []
    embed_model = (model or os.environ.get("EMBED_MODEL_ID") or "BAAI/bge-m3").strip()
    vectors: list[list[float]] = []
    batch_size = 32
    for idx in range(0, len(texts), batch_size):
        batch = texts[idx : idx + batch_size]
        resp = requests.post(
            f"{tei_url.rstrip('/')}/v1/embeddings",
            json={"input": batch, "model": embed_model},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        vectors.extend(item["embedding"] for item in data)
    return vectors


def sanitize_metadata(meta: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(meta, dict):
        return {}
    clean: dict[str, Any] = {}
    for k, v in meta.items():
        key = str(k).strip()
        if not key or key in RESERVED_PAYLOAD_KEYS:
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            clean[key] = v
        elif isinstance(v, list):
            clean[key] = [x if isinstance(x, (str, int, float, bool)) else str(x) for x in v[:50]]
        else:
            clean[key] = json.dumps(v, default=str)
    return clean


def compute_content_hash(doc: IngestDocument) -> str:
    if doc.content_hash:
        return doc.content_hash
    h = hashlib.sha256()
    h.update(doc.content_type.encode("utf-8"))
    h.update(b"|")
    h.update((doc.filename or "").encode("utf-8"))
    h.update(b"|")
    if doc.raw_bytes is not None:
        h.update(doc.raw_bytes)
    elif doc.content_type == "chunks":
        h.update(json.dumps([c.model_dump() for c in doc.chunks], sort_keys=True).encode("utf-8"))
    else:
        h.update(doc.content.encode("utf-8"))
    h.update(b"|")
    h.update(json.dumps(sanitize_metadata(doc.metadata), sort_keys=True).encode("utf-8"))
    h.update(b"|")
    h.update(doc.chunking.model_dump_json().encode("utf-8"))
    return f"sha256:{h.hexdigest()}"


# ------------------------------------------------------------------------------
# Qdrant + PostgreSQL Lifecycle Operations
# ------------------------------------------------------------------------------

def ensure_collection_payload_indexes(qdrant: QdrantClient, collection_name: str) -> None:
    for field_name in ("doc_id", "source_id"):
        try:
            qdrant.create_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        except Exception:
            pass


def delete_document_points(
    qdrant: QdrantClient,
    collection_name: str,
    source_key: str,
    doc_id: str,
) -> None:
    qdrant.delete(
        collection_name=collection_name,
        points_selector=Filter(
            must=[
                FieldCondition(key="source_id", match=MatchValue(value=source_key)),
                FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
            ]
        ),
    )


def _decode_binary_content(doc: IngestDocument) -> bytes:
    if doc.raw_bytes is not None:
        return doc.raw_bytes
    if not doc.content:
        return b""
    return base64.b64decode(doc.content.encode("ascii"))


def process_ingest_documents(
    *,
    documents: Iterable[IngestDocument],
    qdrant: QdrantClient,
    db_dsn: str,
    project_slug: str,
    collection_name: str,
    source_key: str,
    source_system: str,
    tika_url: str,
    tei_url: str,
    embed_model: str,
    embed_vector_size: int,
    job_id: int | None = None,
    sync_mode: str = "incremental",
    default_chunking: ChunkConfig | None = None,
) -> IngestStats:
    """Ingest a stream of normalized IngestDocument items into Qdrant and track in Postgres."""
    stats = IngestStats()
    ensure_collection_payload_indexes(qdrant, collection_name)

    with psycopg.connect(db_dsn) as conn:
        for doc in documents:
            stats.docs_seen += 1
            try:
                if doc.action == "delete":
                    delete_document_points(qdrant, collection_name, source_key, doc.doc_id)
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM ingest_documents WHERE source_key = %s AND project_slug = %s AND doc_id = %s",
                            (source_key, project_slug, doc.doc_id),
                        )
                    conn.commit()
                    stats.docs_deleted += 1
                    continue

                if default_chunking and doc.chunking == ChunkConfig():
                    doc_chunking = default_chunking
                else:
                    doc_chunking = doc.chunking

                content_hash = compute_content_hash(doc)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT content_hash FROM ingest_documents WHERE source_key = %s AND project_slug = %s AND doc_id = %s",
                        (source_key, project_slug, doc.doc_id),
                    )
                    existing_row = cur.fetchone()

                if existing_row and existing_row[0] == content_hash:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE ingest_documents SET last_job_id = %s, updated_at = NOW() "
                            "WHERE source_key = %s AND project_slug = %s AND doc_id = %s",
                            (job_id, source_key, project_slug, doc.doc_id),
                        )
                    conn.commit()
                    stats.docs_skipped += 1
                    continue

                # Extract & chunk
                chunk_items: list[tuple[str, dict[str, Any]]] = []
                base_meta = sanitize_metadata(doc.metadata)
                if doc.content_type == "chunks":
                    for pre in doc.chunks:
                        txt = (pre.text or "").strip()
                        if txt:
                            merged = {**base_meta, **sanitize_metadata(pre.metadata)}
                            chunk_items.append((txt, merged))
                elif doc.content_type == "binary":
                    raw = _decode_binary_content(doc)
                    extracted = extract_text_with_tika(tika_url, raw, doc.filename or doc.doc_id) if raw else ""
                    for c_txt in apply_chunking(extracted, doc_chunking):
                        chunk_items.append((c_txt, base_meta))
                else:
                    for c_txt in apply_chunking(doc.content, doc_chunking):
                        chunk_items.append((c_txt, base_meta))

                # Always delete previous points for this doc_id so shrinking/empty docs don't leave stale chunks
                delete_document_points(qdrant, collection_name, source_key, doc.doc_id)

                if chunk_items:
                    vectors = embed_texts(tei_url, [c[0] for c in chunk_items], model=embed_model)
                    if vectors and len(vectors[0]) != embed_vector_size:
                        raise ValueError(
                            f"Embedding dimension mismatch: expected {embed_vector_size}, got {len(vectors[0])}"
                        )
                    points: list[PointStruct] = []
                    display_name = doc.filename or doc.doc_id
                    for idx, ((c_txt, c_meta), vector) in enumerate(zip(chunk_items, vectors)):
                        point_id = str(
                            uuid.uuid5(uuid.NAMESPACE_URL, f"{source_key}:{collection_name}:{doc.doc_id}:{idx}")
                        )
                        payload = {
                            **c_meta,
                            "text": c_txt,
                            "source": source_system,
                            "source_id": source_key,
                            "doc_id": doc.doc_id,
                            "filename": display_name,
                            "chunk_index": idx,
                            "content_hash": content_hash,
                        }
                        points.append(PointStruct(id=point_id, vector=vector, payload=payload))
                    qdrant.upsert(collection_name=collection_name, points=points)
                    stats.chunks_upserted += len(points)

                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ingest_documents (source_key, project_slug, doc_id, filename, content_hash, chunk_count, last_job_id, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (source_key, project_slug, doc_id) DO UPDATE
                        SET filename = EXCLUDED.filename,
                            content_hash = EXCLUDED.content_hash,
                            chunk_count = EXCLUDED.chunk_count,
                            last_job_id = EXCLUDED.last_job_id,
                            updated_at = NOW()
                        """,
                        (source_key, project_slug, doc.doc_id, doc.filename or doc.doc_id, content_hash, len(chunk_items), job_id),
                    )
                conn.commit()
                stats.docs_upserted += 1
            except Exception as exc:
                conn.rollback()
                stats.errors += 1
                msg = f"{doc.doc_id}: {exc}"
                logger.warning("Ingest error for %s (%s): %s", project_slug, doc.doc_id, exc)
                stats.error_messages.append(msg)

        # Full-mirror pruning: delete documents from this source not seen in this run
        if sync_mode == "full_mirror" and job_id is not None and stats.errors == 0:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT doc_id FROM ingest_documents WHERE source_key = %s AND project_slug = %s AND (last_job_id IS NULL OR last_job_id <> %s)",
                    (source_key, project_slug, job_id),
                )
                stale_docs = [r[0] for r in cur.fetchall()]
            for stale_id in stale_docs:
                delete_document_points(qdrant, collection_name, source_key, stale_id)
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM ingest_documents WHERE source_key = %s AND project_slug = %s AND doc_id = %s",
                        (source_key, project_slug, stale_id),
                    )
                conn.commit()
                stats.docs_deleted += 1

    return stats
