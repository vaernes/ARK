#!/usr/bin/env python3
"""
ARK (Air-gapped Retrieval & Knowledge) Multi-Tenant Provisioning CLI.

Idempotently initializes:
1. Shared `Company_Common` and isolated project Qdrant collections.
2. Open WebUI RBAC user group (`<project-slug>`).
3. Open WebUI Knowledge Bases (`Company_Common` + `<Project_Name>`).
4. Dedicated Workspace Assistant model profile bound to both Knowledge Bases
   and locked to the project's RBAC group.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests
import urllib3
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("ark-bootstrap")

COMPANY_COMMON_COLLECTION = "Company_Common"


class ProjectIdentifiers(BaseModel):
    """Normalized identifiers derived from a human-readable project name."""

    raw_name: str
    collection_name: str
    group_name: str
    display_name: str
    model_id: str

    @classmethod
    def from_project_name(cls, project_name: str) -> "ProjectIdentifiers":
        cleaned = project_name.strip()
        if not cleaned:
            raise ValueError("Project name cannot be empty.")

        tokens = [t for t in re.split(r"[^A-Za-z0-9]+", cleaned) if t]
        if not tokens:
            raise ValueError(f"Project name '{project_name}' contains no alphanumeric characters.")

        collection_name = "_".join(tokens)
        group_name = "-".join(t.lower() for t in tokens)
        display_name = " ".join(t.capitalize() if t.islower() else t for t in tokens)
        model_id = f"{group_name}-assistant"

        return cls(
            raw_name=cleaned,
            collection_name=collection_name,
            group_name=group_name,
            display_name=display_name,
            model_id=model_id,
        )


class ProvisioningConfig(BaseModel):
    """Runtime configuration for Qdrant and Open WebUI provisioning."""

    qdrant_url: str = Field(default="http://127.0.0.1:6333")
    qdrant_api_key: str | None = Field(default=None)
    webui_url: str = Field(default="https://ark.local")
    webui_api_key: str = Field(min_length=1)
    base_model: str = Field(default="ark-llm")
    vector_size: int = Field(default=1024, gt=0)
    verify_tls: bool = Field(default=True)


def load_env_file(env_path: Path) -> None:
    """Load key=value pairs from a .env file into os.environ if not already set."""
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def ensure_qdrant_collection(
    client: QdrantClient,
    collection_name: str,
    vector_size: int,
) -> None:
    """Idempotently create a Qdrant collection with cosine distance and on-disk payloads."""
    existing = {c.name for c in client.get_collections().collections}
    if collection_name in existing:
        logger.info("Qdrant collection '%s' already exists.", collection_name)
        return

    logger.info(
        "Creating Qdrant collection '%s' (dim=%d, distance=COSINE, on_disk_payload=True)...",
        collection_name,
        vector_size,
    )
    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        on_disk_payload=True,
    )
    logger.info("Created Qdrant collection '%s'.", collection_name)


class OpenWebUIClient:
    """Minimal, idempotent REST client for Open WebUI RBAC, Knowledge, and Models."""

    def __init__(self, base_url: str, api_key: str, verify_tls: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.verify = verify_tls
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        if not verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        resp = self.session.request(method, url, timeout=30, **kwargs)
        if not resp.ok:
            raise RuntimeError(
                f"Open WebUI API error {resp.status_code} on {method} {path}: {resp.text}"
            )
        if resp.content:
            return resp.json()
        return None

    def ensure_group(self, name: str, description: str) -> dict[str, Any]:
        """Return existing RBAC group by name or create it."""
        groups = self._request("GET", "/api/v1/groups/")
        for grp in groups:
            if grp.get("name") == name:
                logger.info("Open WebUI group '%s' already exists (id=%s).", name, grp.get("id"))
                return grp

        logger.info("Creating Open WebUI RBAC group '%s'...", name)
        created = self._request(
            "POST",
            "/api/v1/groups/create",
            json={
                "name": name,
                "description": description,
                "permissions": {
                    "workspace": {
                        "models": False,
                        "knowledge": True,
                        "prompts": True,
                        "tools": False,
                    },
                    "chat": {
                        "controls": True,
                        "file_upload": True,
                        "delete": True,
                        "edit": True,
                        "temporary": True,
                    },
                },
            },
        )
        logger.info("Created Open WebUI group '%s' (id=%s).", name, created.get("id"))
        return created

    def ensure_knowledge_base(
        self,
        name: str,
        description: str,
        read_group_ids: list[str],
        write_group_ids: list[str],
    ) -> dict[str, Any]:
        """Return existing Knowledge Base by name or create it with RBAC access control."""
        kbs = self._request("GET", "/api/v1/knowledge/")
        for kb in kbs:
            if kb.get("name") == name:
                logger.info("Open WebUI Knowledge Base '%s' already exists (id=%s).", name, kb.get("id"))
                if read_group_ids or write_group_ids:
                    existing_acl = kb.get("access_control") or {}
                    merged_read = sorted(
                        set((existing_acl.get("read") or {}).get("group_ids", []))
                        | set(read_group_ids)
                    )
                    merged_write = sorted(
                        set((existing_acl.get("write") or {}).get("group_ids", []))
                        | set(write_group_ids)
                    )
                    updated_acl = {
                        "read": {"group_ids": merged_read, "user_ids": []},
                        "write": {"group_ids": merged_write, "user_ids": []},
                    }
                    if updated_acl != existing_acl:
                        kb = self._request(
                            "POST",
                            f"/api/v1/knowledge/{kb['id']}/update",
                            json={
                                "name": kb["name"],
                                "description": kb.get("description") or description,
                                "access_control": updated_acl,
                            },
                        )
                        logger.info("Updated RBAC ACL on Knowledge Base '%s'.", name)
                return kb

        logger.info("Creating Open WebUI Knowledge Base '%s'...", name)
        created = self._request(
            "POST",
            "/api/v1/knowledge/create",
            json={
                "name": name,
                "description": description,
                "access_control": {
                    "read": {"group_ids": read_group_ids, "user_ids": []},
                    "write": {"group_ids": write_group_ids, "user_ids": []},
                },
            },
        )
        logger.info("Created Open WebUI Knowledge Base '%s' (id=%s).", name, created.get("id"))
        return created

    def ensure_assistant_model(
        self,
        model_id: str,
        display_name: str,
        base_model_id: str,
        knowledge_items: list[dict[str, Any]],
        group_id: str,
        project_collection: str,
    ) -> dict[str, Any]:
        """Idempotently create or update a Workspace Assistant locked to an RBAC group."""
        models = self._request("GET", "/api/v1/models/")
        existing = next((m for m in models if m.get("id") == model_id), None)

        system_prompt = (
            f"You are the dedicated AI Assistant for {display_name} in an air-gapped enterprise environment. "
            f"Answer strictly from the attached '{COMPANY_COMMON_COLLECTION}' and '{project_collection}' "
            "knowledge bases when project or company context is requested. "
            "Cite source documents clearly and never fabricate information."
        )

        payload = {
            "id": model_id,
            "base_model_id": base_model_id,
            "name": f"{display_name} Assistant",
            "meta": {
                "profile_image_url": "/static/favicon.png",
                "description": (
                    f"Isolated RAG Assistant for {display_name} "
                    f"(Knowledge: {COMPANY_COMMON_COLLECTION} + {project_collection})."
                ),
                "capabilities": {
                    "vision": False,
                    "usage": True,
                    "citations": True,
                },
                "knowledge": [
                    {
                        "id": kb["id"],
                        "name": kb["name"],
                        "type": "collection",
                    }
                    for kb in knowledge_items
                ],
            },
            "params": {
                "system": system_prompt,
                "temperature": 0.2,
            },
            "access_control": {
                "read": {"group_ids": [group_id], "user_ids": []},
                "write": {"group_ids": [], "user_ids": []},
            },
            "is_active": True,
        }

        if existing:
            logger.info("Updating existing Workspace Assistant '%s'...", model_id)
            updated = self._request(
                "POST",
                f"/api/v1/models/model/update?id={model_id}",
                json=payload,
            )
            logger.info("Updated Workspace Assistant '%s'.", model_id)
            return updated

        logger.info("Creating Workspace Assistant '%s'...", model_id)
        created = self._request("POST", "/api/v1/models/create", json=payload)
        logger.info("Created Workspace Assistant '%s'.", model_id)
        return created


def provision_project(cfg: ProvisioningConfig, project_name: str) -> None:
    """Execute end-to-end provisioning for a single isolated project."""
    ids = ProjectIdentifiers.from_project_name(project_name)
    logger.info(
        "Provisioning ARK project '%s' -> collection='%s', group='%s', assistant='%s'",
        ids.raw_name,
        ids.collection_name,
        ids.group_name,
        ids.model_id,
    )

    qdrant = QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_api_key or None, timeout=30)
    ensure_qdrant_collection(qdrant, COMPANY_COMMON_COLLECTION, cfg.vector_size)
    ensure_qdrant_collection(qdrant, ids.collection_name, cfg.vector_size)

    webui = OpenWebUIClient(
        base_url=cfg.webui_url,
        api_key=cfg.webui_api_key,
        verify_tls=cfg.verify_tls,
    )

    group = webui.ensure_group(
        name=ids.group_name,
        description=f"RBAC group for isolated project {ids.display_name}",
    )
    group_id = group["id"]

    common_kb = webui.ensure_knowledge_base(
        name=COMPANY_COMMON_COLLECTION,
        description="Shared read-only company-wide knowledge base synchronized from HQ.",
        read_group_ids=[group_id],
        write_group_ids=[],
    )
    project_kb = webui.ensure_knowledge_base(
        name=ids.collection_name,
        description=f"Strictly isolated knowledge base for {ids.display_name}.",
        read_group_ids=[group_id],
        write_group_ids=[group_id],
    )

    webui.ensure_assistant_model(
        model_id=ids.model_id,
        display_name=ids.display_name,
        base_model_id=cfg.base_model,
        knowledge_items=[common_kb, project_kb],
        group_id=group_id,
        project_collection=ids.collection_name,
    )

    logger.info(
        "Provisioning complete for '%s'. Assign users to group '%s' in Open WebUI Admin.",
        ids.display_name,
        ids.group_name,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Idempotently provision isolated ARK projects across Qdrant and Open WebUI."
    )
    parser.add_argument(
        "--create-project",
        required=True,
        help="Project name to provision (e.g. 'Project_Alpha').",
    )
    parser.add_argument(
        "--env-file",
        default=str(Path(__file__).resolve().parent.parent / ".env"),
        help="Path to .env file (default: <repo>/.env).",
    )
    parser.add_argument("--qdrant-url", default=None, help="Override QDRANT_URL.")
    parser.add_argument("--qdrant-api-key", default=None, help="Override QDRANT_API_KEY.")
    parser.add_argument("--webui-url", default=None, help="Override WEBUI_URL.")
    parser.add_argument("--webui-api-key", default=None, help="Override OPENWEBUI_API_KEY.")
    parser.add_argument("--base-model", default=None, help="Override LLM_SERVED_NAME.")
    parser.add_argument("--vector-size", type=int, default=None, help="Override EMBED_VECTOR_SIZE.")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (useful before Caddy root CA is trusted).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file(Path(args.env_file))

    webui_api_key = args.webui_api_key or os.environ.get("OPENWEBUI_API_KEY", "")
    if not webui_api_key:
        logger.error(
            "OPENWEBUI_API_KEY is required (set in .env or pass --webui-api-key). "
            "Generate an admin API key in Open WebUI -> Settings -> Account -> API Keys."
        )
        return 1

    cfg = ProvisioningConfig(
        qdrant_url=args.qdrant_url or os.environ.get("QDRANT_URL", "http://127.0.0.1:6333"),
        qdrant_api_key=args.qdrant_api_key or os.environ.get("QDRANT_API_KEY") or None,
        webui_url=args.webui_url or os.environ.get("WEBUI_URL", "https://ark.local"),
        webui_api_key=webui_api_key,
        base_model=args.base_model or os.environ.get("LLM_SERVED_NAME", "ark-llm"),
        vector_size=args.vector_size or int(os.environ.get("EMBED_VECTOR_SIZE", "1024")),
        verify_tls=not args.insecure,
    )

    try:
        provision_project(cfg, args.create_project)
    except Exception as exc:
        logger.error("Provisioning failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
