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
import subprocess
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import requests
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams

from audit import AuditLogger
from connectors import CONNECTOR_RUNNERS, embed_texts

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
TEI_EMBED_URL = os.environ.get("TEI_EMBED_URL", "http://tei-embed:80")
TEI_RERANK_URL = os.environ.get("TEI_RERANK_URL", "http://tei-rerank:80")
TIKA_URL = os.environ.get("TIKA_URL", "http://tika:9998")
OPENWEBUI_URL = os.environ.get("OPENWEBUI_INTERNAL_URL", "http://open-webui:8080")
OPENWEBUI_API_KEY = os.environ.get("OPENWEBUI_API_KEY", "")
KEYCLOAK_URL = os.environ.get("KEYCLOAK_INTERNAL_URL", "http://keycloak:8080")
EMBED_VECTOR_SIZE = int(os.environ.get("EMBED_VECTOR_SIZE", "1024"))
DEFAULT_BACKUP_DIR = os.environ.get("ARK_BACKUP_DIR", "/backups")
AUDIT_LOG_FILE = Path(os.environ.get("ARK_AUDIT_LOG_FILE", "/var/log/ark/audit.jsonl"))

# Postgres connection parameters for pg_dump
PG_HOST = os.environ.get("POSTGRES_HOST", "postgres")
PG_PORT = os.environ.get("POSTGRES_PORT", "5432")
PG_USER = os.environ.get("POSTGRES_USER", "ark_webui")
PG_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")

audit = AuditLogger(db_dsn=DB_DSN, log_file=AUDIT_LOG_FILE)
rate_buckets: dict[str, deque[float]] = defaultdict(deque)

app = FastAPI(title="ARK Control Plane & Project API Gateway", docs_url="/admin-console/docs", openapi_url="/admin-console/openapi.json")
app.mount("/admin-console/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


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
                    rate_limit_rpm INTEGER NOT NULL DEFAULT 60,
                    revoked BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

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
                VALUES ('backup_dir', %s)
                ON CONFLICT (key) DO NOTHING
                """,
                (DEFAULT_BACKUP_DIR,),
            )
        conn.commit()


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def get_actor(request: Request) -> str:
    return request.headers.get("X-Forwarded-Email") or request.headers.get("X-Forwarded-User") or "admin"


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


class CreateKeyRequest(BaseModel):
    project_slug: str
    label: str = Field(min_length=1, max_length=80)
    rate_limit_rpm: int = Field(default=60, ge=1, le=6000)


class BackupSettingRequest(BaseModel):
    backup_dir: str = Field(min_length=1)


class CreateConnectorRequest(BaseModel):
    project_slug: str
    connector_type: str = Field(pattern="^(smb_folder|sharepoint|mfiles)$")
    name: str = Field(min_length=1, max_length=100)
    config: dict[str, Any]


@app.get("/admin-console/", response_class=HTMLResponse)
def admin_ui() -> HTMLResponse:
    html_path = APP_DIR / "static" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


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

    return {"services": results, "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/admin-console/api/projects")
def list_projects() -> list[dict[str, Any]]:
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT slug, name, collection_name, group_name, model_id, rag_enabled, system_prompt, created_at "
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
            "created_at": r[7].isoformat(),
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
                "base_model_id": LLM_SERVED_NAME,
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
                INSERT INTO projects (slug, name, collection_name, group_name, model_id, rag_enabled, system_prompt)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE
                SET name = EXCLUDED.name,
                    rag_enabled = EXCLUDED.rag_enabled,
                    system_prompt = EXCLUDED.system_prompt
                """,
                (slug, display, collection if req.rag_enabled else None, slug, model_id, req.rag_enabled, default_prompt),
            )
        conn.commit()

    audit.record(
        "project.create",
        actor=actor,
        project_slug=slug,
        details={"rag_enabled": req.rag_enabled, "webui_status": webui_status},
    )
    return {"slug": slug, "name": display, "collection_name": collection if req.rag_enabled else None, "rag_enabled": req.rag_enabled, "webui_status": webui_status}


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
                "INSERT INTO api_keys (project_slug, label, key_prefix, key_hash, rate_limit_rpm) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (req.project_slug, req.label, prefix, key_hash, req.rate_limit_rpm),
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
    target = req.backup_dir.strip()
    Path(target).mkdir(parents=True, exist_ok=True)
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
    backup_root = Path(get_backup_setting()["backup_dir"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = backup_root / f"ark-backup-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PGPASSWORD"] = PG_PASSWORD

    dumped_dbs: list[str] = []
    for db_name in ("openwebui", "keycloak", "ark_admin"):
        out_file = dest / f"{db_name}.sql.gz"
        cmd = f"pg_dump -h {PG_HOST} -p {PG_PORT} -U {PG_USER} {db_name} | gzip > {out_file}"
        subprocess.run(["bash", "-c", cmd], env=env, check=True)
        dumped_dbs.append(str(out_file))

    qdrant = get_qdrant()
    snapshots: list[str] = []
    for col in qdrant.get_collections().collections:
        snap = qdrant.create_snapshot(collection_name=col.name)
        snapshots.append(f"{col.name}:{snap.name}")

    audit.record("backup.run", actor=actor, details={"destination": str(dest), "databases": dumped_dbs, "qdrant_snapshots": snapshots})
    return {"status": "ok", "destination": str(dest), "databases": dumped_dbs, "qdrant_snapshots": snapshots}


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
                SELECT k.id, k.label, k.rate_limit_rpm, k.revoked, p.collection_name, p.rag_enabled, p.system_prompt, p.model_id
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

    key_id, label, rpm, _, collection_name, rag_enabled, system_prompt, model_id = row
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

    forwarded_messages = [{"role": "system", "content": sys_content}] + [m for m in messages if m.get("role") != "system"]
    vllm_payload = {**body, "model": LLM_SERVED_NAME, "messages": forwarded_messages, "stream": stream}
    vllm_headers = {"Authorization": f"Bearer {VLLM_API_KEY}", "Content-Type": "application/json"}

    if stream:
        resp = requests.post(f"{VLLM_URL}/v1/chat/completions", headers=vllm_headers, json=vllm_payload, stream=True, timeout=180)
        audit.record(
            "api.chat_completion",
            actor=f"key:{auth['key_label']}",
            project_slug=slug,
            status="success" if resp.ok else f"http_{resp.status_code}",
            details={"rag_enabled": auth["rag_enabled"], "context_chunks": len(context_blocks), "stream": True},
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
        details={"rag_enabled": auth["rag_enabled"], "context_chunks": len(context_blocks), "stream": False},
    )
    return JSONResponse(status_code=resp.status_code, content=resp.json())
