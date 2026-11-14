"""
ARK LLM Backend Management Console (`ark-llm-admin`)
====================================================
Standalone control plane for the ARK vLLM OpenAI-compatible inference server:
- Live vLLM health and served model inspection (/v1/models)
- Local offline model weight inventory (/models)
- Offline model archive staging (.tar.zst, .tar.gz, .tar)
- Switch-plan generation for ./scripts/update-llm.sh
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import requests
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("ark-llm-admin")

APP_DIR = Path(__file__).resolve().parent
ARK_VERSION = os.environ.get("ARK_VERSION", "2026.11.0")
VLLM_URL = os.environ.get("VLLM_URL", "http://vllm:8000").strip().rstrip("/")
LLM_API_KEY = (os.environ.get("LLM_API_KEY") or os.environ.get("VLLM_API_KEY") or "").strip()
LLM_ADMIN_TOKEN = (os.environ.get("LLM_ADMIN_TOKEN") or LLM_API_KEY).strip()
LLM_MODEL_ID = os.environ.get("LLM_MODEL_ID", "Qwen/Qwen2.5-14B-Instruct")
LLM_MODEL_DIR = os.environ.get("LLM_MODEL_DIR", "Qwen--Qwen2.5-14B-Instruct")
LLM_SERVED_NAME = os.environ.get("LLM_SERVED_NAME", "ark-llm")
ARK_MODELS_DIR = Path(os.environ.get("ARK_MODELS_DIR", "/models"))

app = FastAPI(
    title="ARK LLM Backend Management Console",
    version=ARK_VERSION,
    docs_url="/docs",
    openapi_url="/openapi.json",
)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def require_admin_token(request: Request) -> None:
    if not LLM_ADMIN_TOKEN:
        return
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    if not token or not hmac.compare_digest(token, LLM_ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Valid LLM Backend admin token required.")


def _validate_model_dir_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or ".." in cleaned or cleaned.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid model directory name.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", cleaned):
        raise HTTPException(status_code=400, detail="Model directory name contains unsupported characters.")
    return cleaned


def _scan_local_models() -> list[dict[str, Any]]:
    if not ARK_MODELS_DIR.is_dir():
        return []
    models: list[dict[str, Any]] = []
    for entry in sorted(ARK_MODELS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        cfg_file = entry / "config.json"
        arch: list[str] = []
        model_type = ""
        if cfg_file.is_file():
            try:
                cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
                arch = list(cfg.get("architectures") or [])
                model_type = str(cfg.get("model_type") or "")
            except Exception:
                pass
        total_bytes = 0
        weight_files = 0
        for f in entry.rglob("*"):
            if f.is_file():
                total_bytes += f.stat().st_size
                if f.suffix in {".safetensors", ".bin", ".gguf", ".pt"}:
                    weight_files += 1
        models.append(
            {
                "dir": entry.name,
                "active": entry.name == LLM_MODEL_DIR,
                "has_config": cfg_file.is_file(),
                "architectures": arch,
                "model_type": model_type,
                "weight_files": weight_files,
                "size_gib": round(total_bytes / (1024**3), 2),
            }
        )
    return models


class ModelSwitchPlanRequest(BaseModel):
    model_dir: str = Field(min_length=1)
    served_name: str = ""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((APP_DIR / "static" / "index.html").read_text(encoding="utf-8"))


@app.get("/health")
@app.get("/healthz")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": ARK_VERSION}


@app.get("/api/status", dependencies=[Depends(require_admin_token)])
def get_status() -> dict[str, Any]:
    vllm_health = "unreachable"
    try:
        r = requests.get(f"{VLLM_URL}/health", timeout=3)
        vllm_health = "healthy" if r.ok else f"http_{r.status_code}"
    except Exception:
        pass

    vllm_live: dict[str, Any] = {"reachable": False, "models": [], "error": ""}
    try:
        headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}
        resp = requests.get(f"{VLLM_URL}/v1/models", headers=headers, timeout=5)
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
        "version": ARK_VERSION,
        "auth_required": bool(LLM_ADMIN_TOKEN),
        "vllm_health": vllm_health,
        "vllm_live": vllm_live,
        "configured": {
            "vllm_url": VLLM_URL,
            "llm_model_id": LLM_MODEL_ID,
            "llm_model_dir": LLM_MODEL_DIR,
            "llm_served_name": LLM_SERVED_NAME,
            "models_dir": str(ARK_MODELS_DIR),
        },
        "local_models": _scan_local_models(),
    }


@app.post("/api/models/switch-plan", dependencies=[Depends(require_admin_token)])
def model_switch_plan(req: ModelSwitchPlanRequest) -> dict[str, Any]:
    safe_dir = _validate_model_dir_name(req.model_dir)
    if not (ARK_MODELS_DIR / safe_dir).is_dir():
        raise HTTPException(status_code=404, detail=f"Model directory '{safe_dir}' not found in {ARK_MODELS_DIR}.")
    cmd = f"./scripts/update-llm.sh --type llm --model-dir {safe_dir}"
    if req.served_name.strip():
        cmd += f" --served-name {req.served_name.strip()}"
    rollback = f"./scripts/update-llm.sh --type llm --model-dir {LLM_MODEL_DIR}"
    return {
        "model_dir": safe_dir,
        "command": cmd,
        "rollback_command": rollback,
    }


@app.delete("/api/models/{model_dir}", dependencies=[Depends(require_admin_token)])
def delete_local_model(model_dir: str) -> dict[str, str]:
    safe_dir = _validate_model_dir_name(model_dir)
    if safe_dir == LLM_MODEL_DIR:
        raise HTTPException(status_code=409, detail=f"Cannot delete active LLM directory '{safe_dir}'.")
    target = (ARK_MODELS_DIR / safe_dir).resolve()
    if ARK_MODELS_DIR.resolve() not in target.parents or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"Model directory '{safe_dir}' not found.")
    shutil.rmtree(target)
    return {"status": "deleted", "model_dir": safe_dir}


@app.post("/api/models/upload", dependencies=[Depends(require_admin_token)])
async def upload_model_archive(file: UploadFile = File(...)) -> dict[str, Any]:
    filename = Path(file.filename or "model.tar.zst").name
    lower = filename.lower()
    if not (lower.endswith(".tar.zst") or lower.endswith(".tzst") or lower.endswith(".tar.gz") or lower.endswith(".tgz") or lower.endswith(".tar")):
        raise HTTPException(status_code=400, detail="Unsupported archive format. Use .tar.zst, .tar.gz, or .tar.")

    ARK_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ark-llm-upload-") as tmp:
        tmp_path = Path(tmp)
        archive_path = tmp_path / filename
        with archive_path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                out.write(chunk)

        tar_path = archive_path
        if lower.endswith(".tar.zst") or lower.endswith(".tzst"):
            tar_path = tmp_path / "unpacked.tar"
            proc = subprocess.run(["zstd", "-d", "-q", "-o", str(tar_path), str(archive_path)], capture_output=True, text=True)
            if proc.returncode != 0:
                raise HTTPException(status_code=400, detail=f"zstd decompression failed: {proc.stderr.strip()}")

        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        try:
            with tarfile.open(tar_path, "r:*") as tf:
                for member in tf.getmembers():
                    member_path = (extract_dir / member.name).resolve()
                    if extract_dir.resolve() not in member_path.parents and member_path != extract_dir.resolve():
                        raise HTTPException(status_code=400, detail="Archive contains unsafe path traversal.")
                    if member.issym() or member.islnk():
                        raise HTTPException(status_code=400, detail="Archive contains disallowed symlink/hardlink.")
                tf.extractall(extract_dir)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid tar archive: {exc}")

        top_entries = [p for p in extract_dir.iterdir() if not p.name.startswith(".")]
        if len(top_entries) == 1 and top_entries[0].is_dir():
            model_src = top_entries[0]
            model_name = _validate_model_dir_name(model_src.name)
        else:
            stem = re.sub(r"\.(tar\.zst|tzst|tar\.gz|tgz|tar)$", "", filename, flags=re.IGNORECASE)
            model_name = _validate_model_dir_name(stem)
            model_src = extract_dir

        dest = ARK_MODELS_DIR / model_name
        if dest.exists():
            raise HTTPException(status_code=409, detail=f"Model directory '{model_name}' already exists.")
        shutil.move(str(model_src), str(dest))

    return {"status": "staged", "model_dir": model_name}
