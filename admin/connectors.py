"""
ARK Offline Document Ingestion Connectors (M-Files, SharePoint On-Prem, SMB/Folder).

Extracts document text via Apache Tika, chunks text, generates embeddings via
TEI (BAAI/bge-m3), and upserts vectors into the project's isolated Qdrant collection.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

import requests
from qdrant_client import QdrantClient
from qdrant_client.http.models import PointStruct
from requests_ntlm import HttpNtlmAuth

logger = logging.getLogger("ark-connectors")

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".html"}


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
    while start < length:
        end = min(start + chunk_size, length)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    return chunks


def embed_texts(tei_url: str, texts: list[str], model: str = "BAAI/bge-m3") -> list[list[float]]:
    if not texts:
        return []
    vectors: list[list[float]] = []
    batch_size = 32
    for idx in range(0, len(texts), batch_size):
        batch = texts[idx : idx + batch_size]
        resp = requests.post(
            f"{tei_url.rstrip('/')}/v1/embeddings",
            json={"input": batch, "model": model},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        vectors.extend(item["embedding"] for item in data)
    return vectors


def ingest_document(
    *,
    qdrant: QdrantClient,
    collection_name: str,
    tika_url: str,
    tei_url: str,
    source_system: str,
    doc_id: str,
    filename: str,
    content: bytes,
) -> int:
    """Extract, chunk, embed, and upsert a single document into Qdrant."""
    text = extract_text_with_tika(tika_url, content, filename)
    chunks = chunk_text(text)
    if not chunks:
        return 0

    vectors = embed_texts(tei_url, chunks)
    points: list[PointStruct] = []
    for idx, (chunk, vector) in enumerate(zip(chunks, vectors)):
        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source_system}:{collection_name}:{doc_id}:{idx}"))
        points.append(
            PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "text": chunk,
                    "source": source_system,
                    "doc_id": doc_id,
                    "filename": filename,
                    "chunk_index": idx,
                },
            )
        )

    qdrant.upsert(collection_name=collection_name, points=points)
    return len(points)


def sync_smb_folder(
    *,
    config: dict[str, Any],
    qdrant: QdrantClient,
    collection_name: str,
    tika_url: str,
    tei_url: str,
) -> dict[str, int]:
    root = Path(config["path"])
    if not root.is_dir():
        raise FileNotFoundError(f"SMB/Folder path does not exist: {root}")

    files_processed = 0
    chunks_upserted = 0
    for file_path in root.rglob("*"):
        if not file_path.is_file() or file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        rel_id = str(file_path.relative_to(root))
        chunks_upserted += ingest_document(
            qdrant=qdrant,
            collection_name=collection_name,
            tika_url=tika_url,
            tei_url=tei_url,
            source_system="smb_folder",
            doc_id=rel_id,
            filename=file_path.name,
            content=file_path.read_bytes(),
        )
        files_processed += 1

    return {"files_processed": files_processed, "chunks_upserted": chunks_upserted}


def sync_sharepoint_onprem(
    *,
    config: dict[str, Any],
    qdrant: QdrantClient,
    collection_name: str,
    tika_url: str,
    tei_url: str,
) -> dict[str, int]:
    site_url = config["site_url"].rstrip("/")
    folder_url = config["folder_server_relative_url"]
    username = config["username"]
    password = config["password"]
    verify_tls = bool(config.get("verify_tls", True))

    session = requests.Session()
    session.auth = HttpNtlmAuth(username, password)
    session.verify = verify_tls
    session.headers.update({"Accept": "application/json;odata=verbose"})

    api_url = f"{site_url}/_api/web/GetFolderByServerRelativeUrl('{folder_url}')/Files"
    resp = session.get(api_url, timeout=60)
    resp.raise_for_status()
    files = resp.json().get("d", {}).get("results", [])

    files_processed = 0
    chunks_upserted = 0
    for item in files:
        name = item.get("Name", "")
        if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        srv_rel = item.get("ServerRelativeUrl")
        dl_resp = session.get(f"{site_url}/_api/web/GetFileByServerRelativeUrl('{srv_rel}')/$value", timeout=120)
        dl_resp.raise_for_status()
        chunks_upserted += ingest_document(
            qdrant=qdrant,
            collection_name=collection_name,
            tika_url=tika_url,
            tei_url=tei_url,
            source_system="sharepoint",
            doc_id=srv_rel,
            filename=name,
            content=dl_resp.content,
        )
        files_processed += 1

    return {"files_processed": files_processed, "chunks_upserted": chunks_upserted}


def sync_mfiles(
    *,
    config: dict[str, Any],
    qdrant: QdrantClient,
    collection_name: str,
    tika_url: str,
    tei_url: str,
) -> dict[str, int]:
    base_url = config["base_url"].rstrip("/")
    vault_guid = config["vault_guid"]
    username = config["username"]
    password = config["password"]
    verify_tls = bool(config.get("verify_tls", True))
    object_type = int(config.get("object_type", 0))

    session = requests.Session()
    session.verify = verify_tls

    auth_resp = session.post(
        f"{base_url}/REST/server/authenticationtokens",
        json={"Username": username, "Password": password, "VaultGuid": vault_guid},
        timeout=30,
    )
    auth_resp.raise_for_status()
    token = auth_resp.json()["Value"]
    session.headers.update({"X-Authentication": token})

    search_resp = session.get(f"{base_url}/REST/objects?o={object_type}&limit=200", timeout=60)
    search_resp.raise_for_status()
    items = search_resp.json().get("Items", [])

    files_processed = 0
    chunks_upserted = 0
    for obj in items:
        obj_ver = obj.get("ObjVer", {})
        obj_type = obj_ver.get("Type")
        obj_id = obj_ver.get("ID")
        ver = obj_ver.get("Version")
        for f in obj.get("Files", []):
            title = f.get("Name", "")
            ext = f".{f.get('Extension', '').lstrip('.')}".lower()
            filename = f"{title}{ext}" if ext != "." else title
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            file_id = f.get("ID")
            dl_resp = session.get(
                f"{base_url}/REST/objects/{obj_type}/{obj_id}/{ver}/files/{file_id}/content",
                timeout=120,
            )
            dl_resp.raise_for_status()
            chunks_upserted += ingest_document(
                qdrant=qdrant,
                collection_name=collection_name,
                tika_url=tika_url,
                tei_url=tei_url,
                source_system="mfiles",
                doc_id=f"{obj_type}/{obj_id}/{file_id}",
                filename=filename,
                content=dl_resp.content,
            )
            files_processed += 1

    return {"files_processed": files_processed, "chunks_upserted": chunks_upserted}


CONNECTOR_RUNNERS = {
    "smb_folder": sync_smb_folder,
    "sharepoint": sync_sharepoint_onprem,
    "mfiles": sync_mfiles,
}
