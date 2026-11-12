"""
ARK Control Plane & Per-Project API Gateway (`ark-admin`).

Provides:
- Web Admin Console (/admin-console/) protected by Keycloak + oauth2-proxy
- Project Lifecycle Management (RAG-enabled and clean non-RAG projects)
- Scoped Per-Project OpenAI API Gateway (/projects/{slug}/v1/chat/completions)
- Company_Common Qdrant Snapshot Upload
- Live Service Health Dashboard
- Configurable Backup & Restore (runtime-editable backup directory)
- M-Files, SharePoint On-Prem, and SMB/Folder Document Connectors
- Local + SIEM Syslog Audit Logging
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
import requests
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams

from audit import AuditLogger
from backup_utils import (
    dump_postgres_gzip,
    restore_postgres_gzip,
    sha256_file,
    validate_backup_dir,
    validate_backup_name,
    verify_backup_manifest,
)
from connectors import CONNECTOR_RUNNERS, embed_texts
from security import authorize_proxy_request, normalize_groups
from version import get_ark_version, get_version_info

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("ark-admin")

APP_DIR = Path(__file__).resolve().parent
DB_DSN = os.environ["ARK_ADMIN_DB_URL"]
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY") or None
VLLM_URL = os.environ.get("VLLM_URL", "http://vllm:8000")
VLLM_API_KEY = os.environ.get("VLLM_API_KEY", "")
LLM_SERVED_NAME = os.environ.get("LLM_SERVED_NAME", "ark-llm")
LLM_MODEL_ID = os.environ.get("LLM_MODEL_ID", "Qwen/Qwen2.5-14B-Instruct")
LLM_MODEL_DIR = os.environ.get("LLM_MODEL_DIR", "Qwen--Qwen2.5-14B-Instruct")
EMBED_MODEL_DIR = os.environ.get("EMBED_MODEL_DIR", "BAAI--bge-m3")
RERANK_MODEL_DIR = os.environ.get("RERANK_MODEL_DIR", "BAAI--bge-reranker-v2-m3")
ARK_MODELS_DIR = Path(os.environ.get("ARK_MODELS_DIR", "/models"))
TEI_EMBED_URL = os.environ.get("TEI_EMBED_URL", "http://tei-embed:80")
TEI_RERANK_URL = os.environ.get("TEI_RERANK_URL", "http://tei-rerank:80")
TIKA_URL = os.environ.get("TIKA_URL", "http://tika:9998")
OPENWEBUI_URL = os.environ.get("OPENWEBUI_INTERNAL_URL", "http://open-webui:8080")
OPENWEBUI_API_KEY = os.environ.get("OPENWEBUI_API_KEY", "")
KEYCLOAK_URL = os.environ.get("KEYCLOAK_INTERNAL_URL", "http://keycloak:8080")
EMBED_VECTOR_SIZE = int(os.environ.get("EMBED_VECTOR_SIZE", "1024"))
DEFAULT_BACKUP_DIR = os.environ.get("ARK_BACKUP_DIR", "/backups")
AUDIT_LOG_FILE = Path(os.environ.get("ARK_AUDIT_LOG_FILE", "/var/log/ark/audit.jsonl"))
PROXY_AUTH_SECRET = os.environ.get("ARK_PROXY_AUTH_SECRET", "")

# Postgres connection parameters for pg_dump
PG_HOST = os.environ.get("POSTGRES_HOST", "postgres")
PG_PORT = os.environ.get("POSTGRES_PORT", "5432")
PG_USER = os.environ.get("POSTGRES_USER", "ark_webui")
PG_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")

ARK_VERSION = get_ark_version()
audit = AuditLogger(db_dsn=DB_DSN, log_file=AUDIT_LOG_FILE)
rate_buckets: dict[str, deque[float]] = defaultdict(deque)

app = FastAPI(
    title="ARK Control Plane & Project API Gateway",
    version=ARK_VERSION,
    docs_url="/admin-console/docs",
    openapi_url="/admin-console/openapi.json",
)
app.mount("/admin-console/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
app.mount("/portal/static", StaticFiles(directory=str(APP_DIR / "static")), name="portal-static")


@app.middleware("http")
async def enforce_proxy_auth(request: Request, call_next: Any) -> Any:
    allowed, status_code, detail = authorize_proxy_request(
        request.url.path,
        request.headers,
        PROXY_AUTH_SECRET,
    )
    if not allowed:
        logger.warning("Blocked unauthorized request to %s: %s", request.url.path, detail)
        try:
            actor_hint = (request.headers.get("X-Forwarded-Email") or request.headers.get("X-Forwarded-User") or "unknown").strip() or "unknown"
            audit.record(
                "auth.denied",
                actor=actor_hint,
                status="denied",
                details={"path": request.url.path, "reason": detail},
            )
        except Exception:
            pass
        return JSONResponse(status_code=status_code, content={"detail": detail})
    if detail:
        request.state.ark_actor = detail
    return await call_next(request)


def init_db() -> None:
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    slug TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    collection_name TEXT,
                    group_name TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    rag_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    system_prompt TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS api_keys (
                    id SERIAL PRIMARY KEY,
                    project_slug TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
                    label TEXT NOT NULL,
                    key_prefix TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    owner TEXT NOT NULL DEFAULT 'admin',
                    rate_limit_rpm INTEGER NOT NULL DEFAULT 60,
                    revoked BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT 'admin';
                ALTER TABLE projects ADD COLUMN IF NOT EXISTS llm_model TEXT NOT NULL DEFAULT '';

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    project_slug TEXT,
                    status TEXT NOT NULL,
                    details JSONB NOT NULL DEFAULT '{}'::jsonb
                );

                CREATE TABLE IF NOT EXISTS connectors (
                    id SERIAL PRIMARY KEY,
                    project_slug TEXT NOT NULL REFERENCES projects(slug) ON DELETE CASCADE,
                    connector_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    config JSONB NOT NULL,
                    last_sync_at TIMESTAMPTZ,
                    last_status TEXT
                );

                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                INSERT INTO system_settings (key, value)
                VALUES ('backup_dir', %s), ('default_llm_model', %s)
                ON CONFLICT (key) DO NOTHING
                """,
                (DEFAULT_BACKUP_DIR, LLM_SERVED_NAME),
            )
        conn.commit()


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def get_actor(request: Request) -> str:
    actor = (request.headers.get("X-Forwarded-Email") or request.headers.get("X-Forwarded-User") or "").strip()
    if not actor:
        raise HTTPException(status_code=401, detail="Missing authenticated user identity.")
    return actor


def get_user_groups(request: Request) -> set[str]:
    return normalize_groups(request.headers.get("X-Forwarded-Groups", ""))


def user_has_project_access(groups: set[str], slug: str, group_name: str) -> bool:
    return "ark-admins" in groups or slug in groups or group_name in groups


def get_default_llm_model() -> str:
    try:
        with psycopg.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM system_settings WHERE key = 'default_llm_model'")
                row = cur.fetchone()
        if row and row[0].strip():
            return row[0].strip()
    except Exception as exc:
        logger.warning("Failed to read default_llm_model from DB, falling back to env: %s", exc)
    return LLM_SERVED_NAME


def get_effective_llm_model(project_llm_model: str = "") -> str:
    override = (project_llm_model or "").strip()
    return override if override else get_default_llm_model()


def _validate_model_dir_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or ".." in cleaned or cleaned.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid model directory name.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", cleaned):
        raise HTTPException(status_code=400, detail="Model directory name contains unsupported characters.")
    return cleaned


def _scan_local_models() -> list[dict[str, Any]]:
    if not ARK_MODELS_DIR.exists():
        return []
    active_roles = {
        LLM_MODEL_DIR: "llm",
        EMBED_MODEL_DIR: "embed",
        RERANK_MODEL_DIR: "rerank",
    }
    models: list[dict[str, Any]] = []
    for entry in sorted(ARK_MODELS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        size_bytes = 0
        weight_files = 0
        for f in entry.rglob("*"):
            if f.is_file():
                try:
                    size_bytes += f.stat().st_size
                except OSError:
                    pass
                if f.suffix in {".safetensors", ".bin", ".gguf", ".pt"}:
                    weight_files += 1
        cfg_path = entry / "config.json"
        model_type = ""
        architectures: list[str] = []
        max_pos = None
        if cfg_path.is_file():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                model_type = str(cfg.get("model_type") or "")
                architectures = [str(a) for a in (cfg.get("architectures") or [])]
                max_pos = cfg.get("max_position_embeddings")
            except Exception:
                model_type = "invalid-config"
        models.append(
            {
                "dir": entry.name,
                "size_bytes": size_bytes,
                "size_gib": round(size_bytes / (1024 ** 3), 2),
                "weight_files": weight_files,
                "has_config": cfg_path.is_file(),
                "model_type": model_type,
                "architectures": architectures,
                "max_position_embeddings": max_pos,
                "active_role": active_roles.get(entry.name),
            }
        )
    return models


def _safe_extract_tar(archive_path: Path, dest_root: Path) -> list[str]:
    """Safely extract a tar/tar.gz/tar.zst archive into dest_root without path traversal."""
    dest_resolved = dest_root.resolve()
    tar_to_open = archive_path
    decompressed_tar: Path | None = None

    if archive_path.name.endswith((".tar.zst", ".tzst")):
        fd, tmp_name = tempfile.mkstemp(suffix=".tar")
        os.close(fd)
        decompressed_tar = Path(tmp_name)
        subprocess.run(["zstd", "-d", "-f", "-o", str(decompressed_tar), str(archive_path)], check=True)
        tar_to_open = decompressed_tar

    top_dirs: set[str] = set()
    try:
        with tarfile.open(tar_to_open, "r:*") as tf:
            members = tf.getmembers()
            for m in members:
                if m.issym() or m.islnk():
                    raise HTTPException(status_code=400, detail=f"Symlinks/hardlinks not permitted in model archive: {m.name}")
                member_path = (dest_resolved / m.name).resolve()
                if member_path != dest_resolved and dest_resolved not in member_path.parents:
                    raise HTTPException(status_code=400, detail=f"Unsafe path in model archive: {m.name}")
                parts = Path(m.name).parts
                if parts and parts[0] not in (".", ""):
                    top_dirs.add(parts[0])
            tf.extractall(dest_resolved, members=members)
    finally:
        if decompressed_tar and decompressed_tar.exists():
            decompressed_tar.unlink(missing_ok=True)
    return sorted(top_dirs)


def get_qdrant() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=30)


def normalize_project(name: str) -> tuple[str, str, str, str]:
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", name.strip()) if t]
    if not tokens:
        raise HTTPException(status_code=400, detail="Project name must contain alphanumeric characters.")
    slug = "-".join(t.lower() for t in tokens)
    collection = "_".join(tokens)
    display = " ".join(t.capitalize() if t.islower() else t for t in tokens)
    model_id = f"{slug}-assistant"
    return slug, collection, display, model_id


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    rag_enabled: bool = True
    system_prompt: str = ""
    llm_model: str = ""


class DefaultModelRequest(BaseModel):
    model_name: str = Field(min_length=1, max_length=128)


class ProjectModelRequest(BaseModel):
    llm_model: str = Field(default="", max_length=128)


class ModelSwitchPlanRequest(BaseModel):
    model_dir: str = Field(min_length=1, max_length=200)
    model_type: str = Field(default="llm", pattern="^(llm|embed|rerank)$")
    served_name: str = Field(default="", max_length=128)
    max_model_len: int | None = Field(default=None, ge=512, le=1048576)
    gpu_memory_utilization: float | None = Field(default=None, gt=0.1, le=1.0)
    tensor_parallel_size: int | None = Field(default=None, ge=1, le=64)


class CreateKeyRequest(BaseModel):
    project_slug: str
    label: str = Field(min_length=1, max_length=80)
    rate_limit_rpm: int = Field(default=60, ge=1, le=6000)


class BackupSettingRequest(BaseModel):
    backup_dir: str = Field(min_length=1)


class RestoreBackupRequest(BaseModel):
    backup_name: str = Field(min_length=1, max_length=128)
    restore_databases: bool = True
    restore_qdrant: bool = True


class CreateConnectorRequest(BaseModel):
    project_slug: str
    connector_type: str = Field(pattern="^(smb_folder|sharepoint|mfiles)$")
    name: str = Field(min_length=1, max_length=100)
    config: dict[str, Any]


@app.get("/admin-console/", response_class=HTMLResponse)
def admin_ui() -> HTMLResponse:
    html_path = APP_DIR / "static" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/portal/", response_class=HTMLResponse)
@app.get("/portal", response_class=HTMLResponse)
def portal_ui() -> HTMLResponse:
    html_path = APP_DIR / "static" / "portal.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/portal/api/me")
def portal_me(request: Request) -> dict[str, Any]:
    actor = get_actor(request)
    groups = sorted(get_user_groups(request))
    return {"user": actor, "groups": groups, "is_admin": "ark-admins" in groups}


@app.get("/portal/api/version")
def portal_version() -> dict[str, Any]:
    return get_version_info()


@app.get("/portal/api/projects")
def portal_projects(request: Request) -> list[dict[str, Any]]:
    groups = get_user_groups(request)
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT slug, name, group_name, model_id, rag_enabled, created_at "
                "FROM projects ORDER BY created_at DESC"
            )
            rows = cur.fetchall()
    return [
        {
            "slug": r[0],
            "name": r[1],
            "group_name": r[2],
            "model_id": r[3],
            "rag_enabled": r[4],
            "created_at": r[5].isoformat(),
        }
        for r in rows
        if user_has_project_access(groups, r[0], r[2])
    ]


@app.get("/portal/api/keys")
def portal_list_keys(request: Request) -> list[dict[str, Any]]:
    actor = get_actor(request)
    groups = get_user_groups(request)
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT k.id, k.project_slug, k.label, k.key_prefix, k.rate_limit_rpm, k.revoked, k.created_at, p.group_name "
                "FROM api_keys k JOIN projects p ON p.slug = k.project_slug "
                "WHERE k.owner = %s AND k.revoked = FALSE ORDER BY k.created_at DESC",
                (actor,),
            )
            rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "project_slug": r[1],
            "label": r[2],
            "key_prefix": r[3],
            "rate_limit_rpm": r[4],
            "revoked": r[5],
            "created_at": r[6].isoformat(),
        }
        for r in rows
        if user_has_project_access(groups, r[1], r[7])
    ]


@app.post("/portal/api/keys")
def portal_create_key(req: CreateKeyRequest, request: Request) -> dict[str, Any]:
    actor = get_actor(request)
    groups = get_user_groups(request)
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT group_name FROM projects WHERE slug = %s", (req.project_slug,))
            proj = cur.fetchone()
            if not proj or not user_has_project_access(groups, req.project_slug, proj[0]):
                raise HTTPException(status_code=403, detail="You do not have access to this project.")

            raw_key = f"ark_proj_{secrets.token_urlsafe(32)}"
            key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
            prefix = raw_key[:16]
            cur.execute(
                "INSERT INTO api_keys (project_slug, label, key_prefix, key_hash, owner, rate_limit_rpm) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (req.project_slug, req.label, prefix, key_hash, actor, req.rate_limit_rpm),
            )
            key_id = cur.fetchone()[0]
        conn.commit()

    audit.record("apikey.create", actor=actor, project_slug=req.project_slug, details={"key_id": key_id, "label": req.label, "portal": True})
    return {"id": key_id, "api_key": raw_key, "prefix": prefix, "project_slug": req.project_slug}


@app.delete("/portal/api/keys/{key_id}")
def portal_revoke_key(key_id: int, request: Request) -> dict[str, str]:
    actor = get_actor(request)
    groups = get_user_groups(request)
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT project_slug, owner FROM api_keys WHERE id = %s", (key_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="API key not found.")
            if row[1] != actor and "ark-admins" not in groups:
                raise HTTPException(status_code=403, detail="You can only revoke your own API keys.")
            cur.execute("UPDATE api_keys SET revoked = TRUE WHERE id = %s", (key_id,))
        conn.commit()
    audit.record("apikey.revoke", actor=actor, project_slug=row[0], details={"key_id": key_id, "portal": True})
    return {"status": "revoked"}


@app.get("/admin-console/api/health")
def health_dashboard() -> dict[str, Any]:
    checks = {
        "vllm": f"{VLLM_URL}/health",
        "tei_embed": f"{TEI_EMBED_URL}/health",
        "tei_rerank": f"{TEI_RERANK_URL}/health",
        "qdrant": f"{QDRANT_URL}/readyz",
        "tika": f"{TIKA_URL}/tika",
        "open_webui": f"{OPENWEBUI_URL}/health",
        "keycloak": f"{KEYCLOAK_URL.rstrip('/')}/realms/ark/.well-known/openid-configuration",
    }
    results: dict[str, str] = {}
    for name, url in checks.items():
        try:
            r = requests.get(url, timeout=3)
            results[name] = "healthy" if r.ok else f"http_{r.status_code}"
        except Exception:
            results[name] = "unreachable"

    try:
        with psycopg.connect(DB_DSN, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        results["postgres"] = "healthy"
    except Exception:
        results["postgres"] = "unreachable"

    return {"version": ARK_VERSION, "services": results, "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/admin-console/api/version")
def admin_version() -> dict[str, Any]:
    return get_version_info()


@app.get("/admin-console/api/projects")
def list_projects() -> list[dict[str, Any]]:
    default_llm = get_default_llm_model()
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT slug, name, collection_name, group_name, model_id, rag_enabled, system_prompt, llm_model, created_at "
                "FROM projects ORDER BY created_at DESC"
            )
            rows = cur.fetchall()
    return [
        {
            "slug": r[0],
            "name": r[1],
            "collection_name": r[2],
            "group_name": r[3],
            "model_id": r[4],
            "rag_enabled": r[5],
            "system_prompt": r[6],
            "llm_model": r[7],
            "effective_llm_model": r[7].strip() if r[7] and r[7].strip() else default_llm,
            "created_at": r[8].isoformat(),
        }
        for r in rows
    ]


@app.post("/admin-console/api/projects")
def create_project(req: CreateProjectRequest, request: Request) -> dict[str, Any]:
    actor = get_actor(request)
    slug, collection, display, model_id = normalize_project(req.name)
    default_prompt = req.system_prompt.strip() or (
        f"You are the dedicated AI Assistant for {display}. "
        + (
            f"Answer strictly from 'Company_Common' and '{collection}' knowledge bases and cite sources."
            if req.rag_enabled
            else "Operate as a clean, isolated assistant without external document retrieval."
        )
    )

    if req.rag_enabled:
        qdrant = get_qdrant()
        existing = {c.name for c in qdrant.get_collections().collections}
        for col in ("Company_Common", collection):
            if col not in existing:
                qdrant.create_collection(
                    collection_name=col,
                    vectors_config=VectorParams(size=EMBED_VECTOR_SIZE, distance=Distance.COSINE),
                    on_disk_payload=True,
                )

    effective_llm = get_effective_llm_model(req.llm_model)

    # Optionally sync with Open WebUI if OPENWEBUI_API_KEY is configured
    webui_status = "skipped_no_api_key"
    if OPENWEBUI_API_KEY:
        try:
            headers = {"Authorization": f"Bearer {OPENWEBUI_API_KEY}", "Content-Type": "application/json"}
            groups = requests.get(f"{OPENWEBUI_URL}/api/v1/groups/", headers=headers, timeout=15).json()
            grp = next((g for g in groups if g.get("name") == slug), None)
            if not grp:
                grp = requests.post(
                    f"{OPENWEBUI_URL}/api/v1/groups/create",
                    headers=headers,
                    json={"name": slug, "description": f"RBAC group for {display}"},
                    timeout=15,
                ).json()
            grp_id = grp["id"]

            kb_items: list[dict[str, Any]] = []
            if req.rag_enabled:
                kbs = requests.get(f"{OPENWEBUI_URL}/api/v1/knowledge/", headers=headers, timeout=15).json()
                for kb_name, write_ids in (("Company_Common", []), (collection, [grp_id])):
                    kb = next((k for k in kbs if k.get("name") == kb_name), None)
                    if not kb:
                        kb = requests.post(
                            f"{OPENWEBUI_URL}/api/v1/knowledge/create",
                            headers=headers,
                            json={
                                "name": kb_name,
                                "description": f"ARK Knowledge Base: {kb_name}",
                                "access_control": {
                                    "read": {"group_ids": [grp_id], "user_ids": []},
                                    "write": {"group_ids": write_ids, "user_ids": []},
                                },
                            },
                            timeout=15,
                        ).json()
                    kb_items.append({"id": kb["id"], "name": kb["name"], "type": "collection"})

            models = requests.get(f"{OPENWEBUI_URL}/api/v1/models/", headers=headers, timeout=15).json()
            existing_model = next((m for m in models if m.get("id") == model_id), None)
            model_payload = {
                "id": model_id,
                "base_model_id": effective_llm,
                "name": f"{display} Assistant",
                "meta": {
                    "description": f"{'RAG' if req.rag_enabled else 'Clean (No-RAG)'} Assistant for {display}",
                    "capabilities": {"vision": False, "usage": True, "citations": req.rag_enabled},
                    "knowledge": kb_items,
                },
                "params": {"system": default_prompt, "temperature": 0.2},
                "access_control": {"read": {"group_ids": [grp_id], "user_ids": []}, "write": {"group_ids": [], "user_ids": []}},
                "is_active": True,
            }
            if existing_model:
                requests.post(f"{OPENWEBUI_URL}/api/v1/models/model/update?id={model_id}", headers=headers, json=model_payload, timeout=15).raise_for_status()
            else:
                requests.post(f"{OPENWEBUI_URL}/api/v1/models/create", headers=headers, json=model_payload, timeout=15).raise_for_status()
            webui_status = "provisioned"
        except Exception as exc:
            logger.warning("Open WebUI provisioning warning for '%s': %s", slug, exc)
            webui_status = f"warning: {exc}"

    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO projects (slug, name, collection_name, group_name, model_id, rag_enabled, system_prompt, llm_model)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE
                SET name = EXCLUDED.name,
                    rag_enabled = EXCLUDED.rag_enabled,
                    system_prompt = EXCLUDED.system_prompt,
                    llm_model = EXCLUDED.llm_model
                """,
                (slug, display, collection if req.rag_enabled else None, slug, model_id, req.rag_enabled, default_prompt, req.llm_model.strip()),
            )
        conn.commit()

    audit.record(
        "project.create",
        actor=actor,
        project_slug=slug,
        details={"rag_enabled": req.rag_enabled, "llm_model": effective_llm, "webui_status": webui_status},
    )
    return {
        "slug": slug,
        "name": display,
        "collection_name": collection if req.rag_enabled else None,
        "rag_enabled": req.rag_enabled,
        "llm_model": req.llm_model.strip(),
        "effective_llm_model": effective_llm,
        "webui_status": webui_status,
    }


@app.patch("/admin-console/api/projects/{slug}/model")
def update_project_model(slug: str, req: ProjectModelRequest, request: Request) -> dict[str, Any]:
    actor = get_actor(request)
    target = req.llm_model.strip()
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE projects SET llm_model = %s WHERE slug = %s RETURNING model_id",
                (target, slug),
            )
            row = cur.fetchone()
        conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found.")

    effective = get_effective_llm_model(target)
    audit.record(
        "project.model_update",
        actor=actor,
        project_slug=slug,
        details={"llm_model": target, "effective_llm_model": effective},
    )
    return {"slug": slug, "llm_model": target, "effective_llm_model": effective}


@app.delete("/admin-console/api/projects/{slug}")
def delete_project(slug: str, request: Request) -> dict[str, Any]:
    actor = get_actor(request)
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM projects WHERE slug = %s RETURNING name, collection_name, rag_enabled",
                (slug,),
            )
            row = cur.fetchone()
        conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found.")

    display, collection_name, rag_enabled = row
    qdrant_deleted = False
    if rag_enabled and collection_name and collection_name != "Company_Common":
        try:
            get_qdrant().delete_collection(collection_name=collection_name)
            qdrant_deleted = True
        except Exception as exc:
            logger.warning("Qdrant collection cleanup failed for '%s': %s", collection_name, exc)

    audit.record(
        "project.delete",
        actor=actor,
        project_slug=slug,
        details={"name": display, "collection_name": collection_name, "qdrant_deleted": qdrant_deleted},
    )
    return {"status": "deleted", "slug": slug, "qdrant_deleted": qdrant_deleted}


@app.get("/admin-console/api/keys")
def list_keys() -> list[dict[str, Any]]:
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, project_slug, label, key_prefix, rate_limit_rpm, revoked, created_at FROM api_keys ORDER BY created_at DESC")
            rows = cur.fetchall()
    return [
        {"id": r[0], "project_slug": r[1], "label": r[2], "key_prefix": r[3], "rate_limit_rpm": r[4], "revoked": r[5], "created_at": r[6].isoformat()}
        for r in rows
    ]


@app.post("/admin-console/api/keys")
def create_key(req: CreateKeyRequest, request: Request) -> dict[str, Any]:
    actor = get_actor(request)
    raw_key = f"ark_proj_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    prefix = raw_key[:16]

    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys (project_slug, label, key_prefix, key_hash, owner, rate_limit_rpm) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (req.project_slug, req.label, prefix, key_hash, actor, req.rate_limit_rpm),
            )
            key_id = cur.fetchone()[0]
        conn.commit()

    audit.record("apikey.create", actor=actor, project_slug=req.project_slug, details={"key_id": key_id, "label": req.label})
    return {"id": key_id, "api_key": raw_key, "prefix": prefix, "project_slug": req.project_slug}


@app.delete("/admin-console/api/keys/{key_id}")
def revoke_key(key_id: int, request: Request) -> dict[str, str]:
    actor = get_actor(request)
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE api_keys SET revoked = TRUE WHERE id = %s RETURNING project_slug", (key_id,))
            row = cur.fetchone()
        conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="API key not found.")
    audit.record("apikey.revoke", actor=actor, project_slug=row[0], details={"key_id": key_id})
    return {"status": "revoked"}


@app.post("/admin-console/api/snapshots/upload")
async def upload_common_snapshot(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    actor = get_actor(request)
    headers = {"api-key": QDRANT_API_KEY} if QDRANT_API_KEY else {}
    content = await file.read()
    resp = requests.post(
        f"{QDRANT_URL}/collections/Company_Common/snapshots/upload?priority=snapshot",
        headers=headers,
        files={"snapshot": (file.filename or "Company_Common.snapshot", content)},
        timeout=300,
    )
    if not resp.ok:
        audit.record("snapshot.upload", actor=actor, status="error", details={"error": resp.text})
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    audit.record("snapshot.upload", actor=actor, details={"filename": file.filename, "bytes": len(content)})
    return {"status": "ok", "filename": file.filename}


@app.get("/admin-console/api/settings/backup")
def get_backup_setting() -> dict[str, str]:
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM system_settings WHERE key = 'backup_dir'")
            row = cur.fetchone()
    return {"backup_dir": row[0] if row else DEFAULT_BACKUP_DIR}


@app.post("/admin-console/api/settings/backup")
def update_backup_setting(req: BackupSettingRequest, request: Request) -> dict[str, str]:
    actor = get_actor(request)
    try:
        validated = validate_backup_dir(req.backup_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    target = str(validated)
    validated.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO system_settings (key, value, updated_at) VALUES ('backup_dir', %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
                (target,),
            )
        conn.commit()
    audit.record("settings.backup_dir", actor=actor, details={"backup_dir": target})
    return {"backup_dir": target}


@app.post("/admin-console/api/backups/run")
def run_backup(request: Request) -> dict[str, Any]:
    actor = get_actor(request)
    try:
        backup_root = validate_backup_dir(get_backup_setting()["backup_dir"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = backup_root / f"ark-backup-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)

    try:
        dumped_dbs: list[str] = []
        db_entries: list[dict[str, Any]] = []
        for db_name in ("openwebui", "keycloak", "ark_admin"):
            out_file = dest / f"{db_name}.sql.gz"
            entry = dump_postgres_gzip(
                db_name=db_name,
                out_file=out_file,
                pg_host=PG_HOST,
                pg_port=PG_PORT,
                pg_user=PG_USER,
                pg_password=PG_PASSWORD,
            )
            dumped_dbs.append(str(out_file))
            db_entries.append(entry)

        qdrant = get_qdrant()
        qdrant_headers = {"api-key": QDRANT_API_KEY} if QDRANT_API_KEY else {}
        snapshots: list[str] = []
        snapshot_entries: list[dict[str, Any]] = []
        qdrant_dir = dest / "qdrant"
        qdrant_dir.mkdir(parents=True, exist_ok=True)
        for col in qdrant.get_collections().collections:
            snap = qdrant.create_snapshot(collection_name=col.name)
            col_dir = qdrant_dir / col.name
            col_dir.mkdir(parents=True, exist_ok=True)
            snap_dest = col_dir / snap.name
            snap_url = (
                f"{QDRANT_URL.rstrip('/')}/collections/{quote(col.name, safe='')}"
                f"/snapshots/{quote(snap.name, safe='')}"
            )
            with requests.get(snap_url, headers=qdrant_headers, stream=True, timeout=300) as resp:
                resp.raise_for_status()
                with snap_dest.open("wb") as out_fh:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            out_fh.write(chunk)
            snapshots.append(f"{col.name}:{snap.name}")
            snapshot_entries.append(
                {
                    "collection": col.name,
                    "name": snap.name,
                    "path": f"qdrant/{col.name}/{snap.name}",
                    "bytes": snap_dest.stat().st_size,
                    "sha256": sha256_file(snap_dest),
                }
            )

        manifest = {
            "ark_version": ARK_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "databases": db_entries,
            "qdrant_snapshots": snapshot_entries,
        }
        manifest_path = dest / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except Exception as exc:
        audit.record("backup.run", actor=actor, status="error", details={"destination": str(dest), "error": str(exc)})
        raise HTTPException(status_code=500, detail=f"Backup failed: {exc}") from exc

    audit.record(
        "backup.run",
        actor=actor,
        details={
            "destination": str(dest),
            "manifest": str(manifest_path),
            "databases": dumped_dbs,
            "qdrant_snapshots": snapshots,
        },
    )
    return {
        "status": "ok",
        "backup_name": dest.name,
        "destination": str(dest),
        "manifest": str(manifest_path),
        "databases": dumped_dbs,
        "qdrant_snapshots": snapshots,
    }


@app.get("/admin-console/api/backups")
def list_backups() -> list[dict[str, Any]]:
    try:
        backup_root = validate_backup_dir(get_backup_setting()["backup_dir"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not backup_root.is_dir():
        return []

    bundles: list[dict[str, Any]] = []
    for entry in sorted(backup_root.iterdir(), key=lambda p: p.name, reverse=True):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        manifest_path = entry / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                bundles.append(
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "created_at": manifest.get("created_at"),
                        "databases": [d.get("database") for d in manifest.get("databases", [])],
                        "qdrant_snapshots": [s.get("collection") for s in manifest.get("qdrant_snapshots", [])],
                        "has_manifest": True,
                    }
                )
            except Exception:
                bundles.append(
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "created_at": None,
                        "databases": [],
                        "qdrant_snapshots": [],
                        "has_manifest": False,
                    }
                )
    return bundles


@app.post("/admin-console/api/backups/restore")
def restore_backup(req: RestoreBackupRequest, request: Request) -> dict[str, Any]:
    actor = get_actor(request)
    try:
        backup_root = validate_backup_dir(get_backup_setting()["backup_dir"])
        safe_name = validate_backup_name(req.backup_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    backup_dir = (backup_root / safe_name).resolve()
    if backup_root not in backup_dir.parents or not backup_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Backup bundle '{safe_name}' not found.")

    try:
        manifest = verify_backup_manifest(backup_dir)
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as exc:
        audit.record("backup.restore", actor=actor, status="error", details={"backup_name": safe_name, "error": str(exc)})
        raise HTTPException(status_code=400, detail=f"Backup integrity verification failed: {exc}") from exc

    restored_collections: list[str] = []
    restored_databases: list[str] = []
    qdrant_headers = {"api-key": QDRANT_API_KEY} if QDRANT_API_KEY else {}

    try:
        if req.restore_qdrant:
            for snap_entry in manifest.get("qdrant_snapshots", []):
                col_name = snap_entry["collection"]
                snap_file = (backup_dir / snap_entry["path"]).resolve()
                with snap_file.open("rb") as fh:
                    resp = requests.post(
                        f"{QDRANT_URL.rstrip('/')}/collections/{quote(col_name, safe='')}/snapshots/upload?priority=snapshot",
                        headers=qdrant_headers,
                        files={"snapshot": (snap_file.name, fh)},
                        timeout=300,
                    )
                if not resp.ok:
                    raise RuntimeError(f"Qdrant snapshot restore failed for '{col_name}': HTTP {resp.status_code} {resp.text}")
                col_healthy = False
                for _ in range(10):
                    check = requests.get(
                        f"{QDRANT_URL.rstrip('/')}/collections/{quote(col_name, safe='')}",
                        headers=qdrant_headers,
                        timeout=15,
                    )
                    if check.ok:
                        payload = check.json()
                        col_status = payload.get("result", {}).get("status")
                        if payload.get("status") == "ok" and col_status in {"green", "yellow"}:
                            col_healthy = True
                            break
                    time.sleep(0.5)
                if not col_healthy:
                    raise RuntimeError(f"Qdrant collection '{col_name}' failed post-restore health check.")
                restored_collections.append(col_name)

        if req.restore_databases:
            for db_entry in manifest.get("databases", []):
                db_name = db_entry["database"]
                dump_file = (backup_dir / db_entry["file"]).resolve()
                restore_postgres_gzip(
                    db_name=db_name,
                    dump_file=dump_file,
                    pg_host=PG_HOST,
                    pg_port=PG_PORT,
                    pg_user=PG_USER,
                    pg_password=PG_PASSWORD,
                )
                restored_databases.append(db_name)
    except Exception as exc:
        try:
            audit.record("backup.restore", actor=actor, status="error", details={"backup_name": safe_name, "error": str(exc)})
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Restore failed: {exc}") from exc

    audit.record(
        "backup.restore",
        actor=actor,
        details={
            "backup_name": safe_name,
            "restored_databases": restored_databases,
            "restored_collections": restored_collections,
        },
    )
    return {
        "status": "ok",
        "backup_name": safe_name,
        "restored_databases": restored_databases,
        "restored_collections": restored_collections,
    }


@app.get("/admin-console/api/connectors")
def list_connectors() -> list[dict[str, Any]]:
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, project_slug, connector_type, name, last_sync_at, last_status FROM connectors ORDER BY id DESC")
            rows = cur.fetchall()
    return [
        {"id": r[0], "project_slug": r[1], "connector_type": r[2], "name": r[3], "last_sync_at": r[4].isoformat() if r[4] else None, "last_status": r[5]}
        for r in rows
    ]


@app.post("/admin-console/api/connectors")
def create_connector(req: CreateConnectorRequest, request: Request) -> dict[str, Any]:
    actor = get_actor(request)
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO connectors (project_slug, connector_type, name, config) VALUES (%s, %s, %s, %s::jsonb) RETURNING id",
                (req.project_slug, req.connector_type, req.name, json.dumps(req.config)),
            )
            cid = cur.fetchone()[0]
        conn.commit()
    audit.record("connector.create", actor=actor, project_slug=req.project_slug, details={"connector_id": cid, "type": req.connector_type})
    return {"id": cid, "status": "created"}


@app.post("/admin-console/api/connectors/{connector_id}/sync")
def trigger_connector_sync(connector_id: int, request: Request) -> dict[str, Any]:
    actor = get_actor(request)
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.project_slug, c.connector_type, c.config, p.collection_name, p.rag_enabled "
                "FROM connectors c JOIN projects p ON p.slug = c.project_slug WHERE c.id = %s",
                (connector_id,),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Connector not found.")
    slug, ctype, config, collection_name, rag_enabled = row
    if not rag_enabled or not collection_name:
        raise HTTPException(status_code=400, detail="Cannot sync documents to a non-RAG clean project.")

    runner = CONNECTOR_RUNNERS[ctype]
    try:
        stats = runner(config=config, qdrant=get_qdrant(), collection_name=collection_name, tika_url=TIKA_URL, tei_url=TEI_EMBED_URL)
        status_text = f"ok ({stats['files_processed']} files, {stats['chunks_upserted']} chunks)"
    except Exception as exc:
        status_text = f"error: {exc}"
        audit.record("connector.sync", actor=actor, project_slug=slug, status="error", details={"connector_id": connector_id, "error": str(exc)})
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        with psycopg.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE connectors SET last_sync_at = NOW(), last_status = %s WHERE id = %s", (status_text, connector_id))
            conn.commit()

    audit.record("connector.sync", actor=actor, project_slug=slug, details={"connector_id": connector_id, **stats})
    return {"status": "ok", **stats}


@app.get("/admin-console/api/models")
def list_models() -> dict[str, Any]:
    vllm_live: dict[str, Any] = {"reachable": False, "models": [], "error": ""}
    try:
        headers = {"Authorization": f"Bearer {VLLM_API_KEY}"} if VLLM_API_KEY else {}
        resp = requests.get(f"{VLLM_URL.rstrip('/')}/v1/models", headers=headers, timeout=5)
        if resp.ok:
            data = resp.json().get("data", [])
            vllm_live = {
                "reachable": True,
                "models": [m.get("id") for m in data if isinstance(m, dict) and m.get("id")],
                "error": "",
            }
        else:
            vllm_live["error"] = f"HTTP {resp.status_code}"
    except Exception as exc:
        vllm_live["error"] = str(exc)

    return {
        "default_llm_model": get_default_llm_model(),
        "configured": {
            "llm_model_id": LLM_MODEL_ID,
            "llm_model_dir": LLM_MODEL_DIR,
            "llm_served_name": LLM_SERVED_NAME,
            "embed_model_dir": EMBED_MODEL_DIR,
            "rerank_model_dir": RERANK_MODEL_DIR,
            "models_dir": str(ARK_MODELS_DIR),
        },
        "vllm_live": vllm_live,
        "local_models": _scan_local_models(),
    }


@app.post("/admin-console/api/models/default")
def update_default_model(req: DefaultModelRequest, request: Request) -> dict[str, str]:
    actor = get_actor(request)
    model_name = req.model_name.strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="Model name cannot be empty.")

    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO system_settings (key, value, updated_at) VALUES ('default_llm_model', %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
                (model_name,),
            )
        conn.commit()
    audit.record("model.default_update", actor=actor, details={"default_llm_model": model_name})
    return {"default_llm_model": model_name}


@app.post("/admin-console/api/models/upload")
async def upload_model_archive(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    actor = get_actor(request)
    filename = Path(file.filename or "model.tar.gz").name
    if not filename.endswith((".tar.zst", ".tzst", ".tar.gz", ".tgz", ".tar")):
        raise HTTPException(status_code=400, detail="Unsupported archive format. Use .tar.zst, .tar.gz, .tgz, or .tar.")

    ARK_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(suffix=f"-{filename}")
    os.close(fd)
    tmp_path = Path(tmp_path_str)
    total_bytes = 0
    try:
        with tmp_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                out.write(chunk)
        extracted_dirs = _safe_extract_tar(tmp_path, ARK_MODELS_DIR)
    except HTTPException:
        audit.record("model.upload", actor=actor, status="error", details={"filename": filename})
        raise
    except Exception as exc:
        audit.record("model.upload", actor=actor, status="error", details={"filename": filename, "error": str(exc)})
        raise HTTPException(status_code=500, detail=f"Failed to extract model archive: {exc}")
    finally:
        tmp_path.unlink(missing_ok=True)

    audit.record(
        "model.upload",
        actor=actor,
        details={"filename": filename, "bytes": total_bytes, "extracted_dirs": extracted_dirs},
    )
    return {"status": "ok", "filename": filename, "bytes": total_bytes, "extracted_dirs": extracted_dirs}


@app.delete("/admin-console/api/models/{model_dir}")
def delete_local_model(model_dir: str, request: Request) -> dict[str, str]:
    actor = get_actor(request)
    safe_dir = _validate_model_dir_name(model_dir)
    if safe_dir in {LLM_MODEL_DIR, EMBED_MODEL_DIR, RERANK_MODEL_DIR}:
        raise HTTPException(status_code=409, detail=f"Cannot delete active configured model directory '{safe_dir}'.")

    target = (ARK_MODELS_DIR / safe_dir).resolve()
    root_resolved = ARK_MODELS_DIR.resolve()
    if root_resolved not in target.parents or not target.is_dir():
        raise HTTPException(status_code=404, detail="Model directory not found.")

    shutil.rmtree(target)
    audit.record("model.delete", actor=actor, details={"model_dir": safe_dir})
    return {"status": "deleted", "model_dir": safe_dir}


@app.post("/admin-console/api/models/switch-plan")
def generate_model_switch_plan(req: ModelSwitchPlanRequest, request: Request) -> dict[str, Any]:
    actor = get_actor(request)
    safe_dir = _validate_model_dir_name(req.model_dir)
    target = ARK_MODELS_DIR / safe_dir
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"Model directory '{safe_dir}' not found in {ARK_MODELS_DIR}.")

    parts = ["./scripts/update-llm.sh", "--type", req.model_type, "--model-dir", safe_dir]
    if req.model_type == "llm":
        if req.served_name.strip():
            parts.extend(["--served-name", req.served_name.strip()])
        if req.max_model_len is not None:
            parts.extend(["--max-model-len", str(req.max_model_len)])
        if req.gpu_memory_utilization is not None:
            parts.extend(["--gpu-mem", str(req.gpu_memory_utilization)])
        if req.tensor_parallel_size is not None:
            parts.extend(["--tp", str(req.tensor_parallel_size)])

    cmd = " ".join(parts)
    audit.record(
        "model.switch_plan",
        actor=actor,
        details={"model_dir": safe_dir, "model_type": req.model_type, "command": cmd},
    )
    return {
        "model_dir": safe_dir,
        "model_type": req.model_type,
        "command": cmd,
        "rollback_command": f"./scripts/update-llm.sh --type {req.model_type} --rollback",
    }


@app.get("/admin-console/api/audit")
def get_audit_logs(limit: int = 50) -> list[dict[str, Any]]:
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, created_at, event_type, actor, project_slug, status, details FROM audit_logs ORDER BY id DESC LIMIT %s", (min(limit, 500),))
            rows = cur.fetchall()
    return [
        {"id": r[0], "created_at": r[1].isoformat(), "event_type": r[2], "actor": r[3], "project_slug": r[4], "status": r[5], "details": r[6]}
        for r in rows
    ]


# ==============================================================================
# Per-Project OpenAI-Compatible API Gateway (/projects/{slug}/v1/...)
# ==============================================================================

def authenticate_project_key(slug: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer API key.")
    token = authorization.removeprefix("Bearer ").strip()
    key_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT k.id, k.label, k.rate_limit_rpm, k.revoked, p.collection_name, p.rag_enabled, p.system_prompt, p.model_id, p.llm_model
                FROM api_keys k
                JOIN projects p ON p.slug = k.project_slug
                WHERE k.key_hash = %s AND k.project_slug = %s
                """,
                (key_hash, slug),
            )
            row = cur.fetchone()

    if not row or row[3]:
        audit.record("api.auth_failed", actor="unknown", project_slug=slug, status="denied")
        raise HTTPException(status_code=403, detail="Invalid or revoked project API key.")

    key_id, label, rpm, _, collection_name, rag_enabled, system_prompt, model_id, llm_model = row
    now = time.monotonic()
    bucket = rate_buckets[f"{slug}:{key_id}"]
    while bucket and now - bucket[0] > 60.0:
        bucket.popleft()
    if len(bucket) >= rpm:
        audit.record("api.rate_limited", actor=f"key:{label}", project_slug=slug, status="denied")
        raise HTTPException(status_code=429, detail="Project API rate limit exceeded.")
    bucket.append(now)

    return {
        "key_label": label,
        "collection_name": collection_name,
        "rag_enabled": rag_enabled,
        "system_prompt": system_prompt,
        "model_id": model_id,
        "llm_model": llm_model,
    }


@app.get("/projects/{slug}/v1/models")
def project_models(slug: str, auth: dict[str, Any] = Depends(authenticate_project_key)) -> dict[str, Any]:
    return {"object": "list", "data": [{"id": auth["model_id"], "object": "model", "owned_by": f"ark-{slug}"}]}


def rerank_contexts(query: str, candidates: list[dict[str, str]], top_n: int = 4) -> list[str]:
    """Rerank retrieved Qdrant chunks via TEI Reranker, falling back to vector order."""
    if not candidates:
        return []
    ordered = candidates
    if len(candidates) > 1:
        try:
            resp = requests.post(
                f"{TEI_RERANK_URL.rstrip('/')}/rerank",
                json={"query": query, "texts": [c["text"] for c in candidates]},
                timeout=15,
            )
            if resp.ok:
                ranked = sorted(resp.json(), key=lambda r: r.get("score", 0.0), reverse=True)
                ordered = [candidates[r["index"]] for r in ranked if 0 <= r.get("index", -1) < len(candidates)]
        except Exception as exc:
            logger.warning("TEI rerank fallback to vector order: %s", exc)
    return [f"[{c['col']} | {c['src']}]\n{c['text']}" for c in ordered[:top_n]]


@app.post("/projects/{slug}/v1/chat/completions")
async def project_chat_completions(slug: str, request: Request, auth: dict[str, Any] = Depends(authenticate_project_key)) -> Any:
    body = await request.json()
    messages = list(body.get("messages") or [])
    stream = bool(body.get("stream", False))

    context_blocks: list[str] = []
    if auth["rag_enabled"] and messages:
        last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        if isinstance(last_user, str) and last_user.strip():
            query_vec = embed_texts(TEI_EMBED_URL, [last_user])[0]
            qdrant = get_qdrant()
            candidates: list[dict[str, str]] = []
            for col in ("Company_Common", auth["collection_name"]):
                if not col:
                    continue
                try:
                    hits = qdrant.query_points(collection_name=col, query=query_vec, limit=6).points
                    for hit in hits:
                        payload = hit.payload or {}
                        text = payload.get("text", "")
                        src = payload.get("filename", col)
                        if text:
                            candidates.append({"col": col, "src": src, "text": text})
                except Exception as exc:
                    logger.warning("Qdrant search failed on '%s': %s", col, exc)
            context_blocks = rerank_contexts(last_user, candidates, top_n=4)

    sys_content = auth["system_prompt"]
    if context_blocks:
        sys_content += "\n\nRetrieved Context:\n" + "\n---\n".join(context_blocks)

    target_model = get_effective_llm_model(auth.get("llm_model", ""))
    forwarded_messages = [{"role": "system", "content": sys_content}] + [m for m in messages if m.get("role") != "system"]
    vllm_payload = {**body, "model": target_model, "messages": forwarded_messages, "stream": stream}
    vllm_headers = {"Authorization": f"Bearer {VLLM_API_KEY}", "Content-Type": "application/json"}

    if stream:
        resp = requests.post(f"{VLLM_URL}/v1/chat/completions", headers=vllm_headers, json=vllm_payload, stream=True, timeout=180)
        audit.record(
            "api.chat_completion",
            actor=f"key:{auth['key_label']}",
            project_slug=slug,
            status="success" if resp.ok else f"http_{resp.status_code}",
            details={"rag_enabled": auth["rag_enabled"], "context_chunks": len(context_blocks), "stream": True, "llm_model": target_model},
        )
        if not resp.ok:
            return JSONResponse(status_code=resp.status_code, content=resp.json())

        def event_stream():
            try:
                for line in resp.iter_lines(decode_unicode=False):
                    if line:
                        yield line + b"\n\n"
            finally:
                resp.close()

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    resp = requests.post(f"{VLLM_URL}/v1/chat/completions", headers=vllm_headers, json=vllm_payload, timeout=180)
    audit.record(
        "api.chat_completion",
        actor=f"key:{auth['key_label']}",
        project_slug=slug,
        status="success" if resp.ok else f"http_{resp.status_code}",
        details={"rag_enabled": auth["rag_enabled"], "context_chunks": len(context_blocks), "stream": False, "llm_model": target_model},
    )
    return JSONResponse(status_code=resp.status_code, content=resp.json())
