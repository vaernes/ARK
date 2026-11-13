"""
ARK Declarative Ingest Pipeline Engine (`ark-ingest/v1`).

Executes YAML/JSON ingestion pipelines defined by ARK administrators without
arbitrary code execution:
- Source adapters: `http_json`, `http_paginated`, `sql_query`, `filesystem_glob`, `csv`, `ndjson`
- Safe template rendering via `jinja2.sandbox.SandboxedEnvironment`
- Pure built-in filters: `html_to_text`, `slugify`, `strip`, `json_get`, `regex_replace`, `join`
- Supports Dry-Run preview (returning up to N normalized `IngestDocument` items)
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import psycopg
import requests
import yaml
from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, Field
from requests_ntlm import HttpNtlmAuth

from ingest import SUPPORTED_EXTENSIONS, ChunkConfig, IngestDocument, apply_chunking

SUPPORTED_SOURCE_TYPES = {"http_json", "http_paginated", "sql_query", "filesystem_glob", "csv", "ndjson"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"}:
            self._skip = False
        elif tag.lower() in {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip and data:
            self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def filter_html_to_text(value: Any) -> str:
    parser = _TextExtractor()
    parser.feed(str(value or ""))
    parser.close()
    return parser.get_text()


def filter_slugify(value: Any) -> str:
    tokens = [t.lower() for t in re.split(r"[^A-Za-z0-9]+", str(value or "")) if t]
    return "-".join(tokens) or "doc"


def filter_json_get(value: Any, path: str, default: Any = "") -> Any:
    cur = value
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def filter_regex_replace(value: Any, pattern: str, replacement: str = "") -> str:
    return re.sub(pattern, replacement, str(value or ""))


def create_sandbox_env() -> SandboxedEnvironment:
    env = SandboxedEnvironment(autoescape=False)
    env.filters["html_to_text"] = filter_html_to_text
    env.filters["slugify"] = filter_slugify
    env.filters["strip"] = lambda v: str(v or "").strip()
    env.filters["json_get"] = filter_json_get
    env.filters["regex_replace"] = filter_regex_replace
    env.filters["join"] = lambda v, sep=", ": sep.join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v or "")
    return env


class PipelineTransform(BaseModel):
    doc_id: str = "{{ item.id }}"
    filename: str = "{{ item.id }}.md"
    content_type: str = "text"
    template: str = "{{ item.content | default('') }}"
    metadata: dict[str, str] = Field(default_factory=dict)


class PipelineSpec(BaseModel):
    apiVersion: str = "ark-ingest/v1"
    name: str = "pipeline"
    sync_mode: str = "incremental"
    source: dict[str, Any]
    transform: PipelineTransform = Field(default_factory=PipelineTransform)
    chunking: ChunkConfig = Field(default_factory=ChunkConfig)


def parse_pipeline_spec(raw_spec: str) -> PipelineSpec:
    data = yaml.safe_load(raw_spec)
    if not isinstance(data, dict):
        raise ValueError("Pipeline spec must be a YAML/JSON mapping.")
    spec = PipelineSpec.model_validate(data)
    if spec.apiVersion != "ark-ingest/v1":
        raise ValueError(f"Unsupported apiVersion '{spec.apiVersion}'; expected 'ark-ingest/v1'.")
    stype = str(spec.source.get("type") or "").strip()
    if stype not in SUPPORTED_SOURCE_TYPES:
        raise ValueError(f"Unsupported source.type '{stype}'. Supported: {sorted(SUPPORTED_SOURCE_TYPES)}")
    return spec


def _resolve_secret(val: Any, secrets: dict[str, Any]) -> str:
    s = str(val or "")
    if s.startswith("secret:"):
        key = s.removeprefix("secret:").strip()
        return str(secrets.get(key, ""))
    return s


def _build_http_session(source: dict[str, Any], secrets: dict[str, Any]) -> requests.Session:
    session = requests.Session()
    session.verify = bool(source.get("verify_tls", True))
    headers = dict(source.get("headers") or {})
    auth_cfg = source.get("auth") or {}
    auth_type = str(auth_cfg.get("type") or "").lower()
    if auth_type == "bearer":
        tok = _resolve_secret(auth_cfg.get("token") or auth_cfg.get("token_secret") or "", secrets)
        if not tok and auth_cfg.get("token_secret"):
            tok = str(secrets.get(auth_cfg["token_secret"], ""))
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
    elif auth_type == "basic":
        session.auth = (
            _resolve_secret(auth_cfg.get("username", ""), secrets),
            _resolve_secret(auth_cfg.get("password", ""), secrets),
        )
    elif auth_type == "ntlm":
        session.auth = HttpNtlmAuth(
            _resolve_secret(auth_cfg.get("username", ""), secrets),
            _resolve_secret(auth_cfg.get("password", ""), secrets),
        )
    elif auth_type == "header":
        hname = str(auth_cfg.get("header_name") or "X-API-Key")
        headers[hname] = _resolve_secret(auth_cfg.get("value", ""), secrets)
    session.headers.update(headers)
    return session


def _fetch_source_items(
    source: dict[str, Any],
    secrets: dict[str, Any],
    limit: int | None = None,
) -> list[dict[str, Any]]:
    stype = source["type"]
    items: list[dict[str, Any]] = []

    if stype in {"http_json", "http_paginated"}:
        session = _build_http_session(source, secrets)
        url = str(source["url"])
        items_path = str(source.get("items_path") or "")
        pagination = source.get("pagination") or {}
        page_limit = int(pagination.get("limit") or 50)
        max_pages = int(pagination.get("max_pages") or (2 if limit else 100))
        offset = int(pagination.get("start_offset") or 0)

        for _ in range(max_pages if stype == "http_paginated" else 1):
            params = dict(source.get("params") or {})
            if stype == "http_paginated":
                params[str(pagination.get("offset_param") or "offset")] = offset
                params[str(pagination.get("limit_param") or "limit")] = page_limit
            resp = session.get(url, params=params, timeout=60)
            resp.raise_for_status()
            payload = resp.json()
            batch = filter_json_get(payload, items_path, default=[]) if items_path else payload
            if not isinstance(batch, list):
                batch = [batch] if batch is not None else []
            if not batch:
                break
            for row in batch:
                items.append(row if isinstance(row, dict) else {"value": row})
                if limit is not None and len(items) >= limit:
                    return items
            if len(batch) < page_limit:
                break
            offset += len(batch)
        return items

    if stype == "sql_query":
        dsn = _resolve_secret(source.get("dsn") or "", secrets)
        if not dsn and source.get("dsn_secret"):
            dsn = str(secrets.get(source["dsn_secret"], ""))
        query = str(source["query"])
        if dsn.startswith("sqlite:///"):
            db_path = dsn.removeprefix("sqlite:///")
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                cur = conn.cursor()
                cur.execute(query)
                rows = cur.fetchmany(limit) if limit is not None else cur.fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                cols = [d.name for d in (cur.description or [])]
                rows = cur.fetchmany(limit) if limit is not None else cur.fetchall()
                return [dict(zip(cols, r)) for r in rows]

    if stype == "filesystem_glob":
        root = Path(str(source["path"]))
        if not root.is_dir():
            raise FileNotFoundError(f"Directory not found: {root}")
        pattern = str(source.get("glob") or "**/*")
        for fpath in sorted(root.glob(pattern)):
            if not fpath.is_file():
                continue
            rel = str(fpath.relative_to(root))
            is_text = fpath.suffix.lower() in {".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml", ".html"}
            if not is_text and fpath.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            items.append(
                {
                    "id": rel,
                    "filename": fpath.name,
                    "path": rel,
                    "content": fpath.read_text(encoding="utf-8", errors="replace") if is_text else "",
                    "_raw_bytes": None if is_text else fpath.read_bytes(),
                }
            )
            if limit is not None and len(items) >= limit:
                break
        return items

    if stype == "csv":
        if source.get("path"):
            text = Path(str(source["path"])).read_text(encoding="utf-8", errors="replace")
        elif source.get("url"):
            resp = _build_http_session(source, secrets).get(str(source["url"]), timeout=60)
            resp.raise_for_status()
            text = resp.text
        else:
            text = str(source.get("data") or "")
        reader = csv.DictReader(io.StringIO(text), delimiter=str(source.get("delimiter") or ","))
        for idx, row in enumerate(reader):
            row.setdefault("id", str(idx))
            items.append(dict(row))
            if limit is not None and len(items) >= limit:
                break
        return items

    if stype == "ndjson":
        if source.get("path"):
            text = Path(str(source["path"])).read_text(encoding="utf-8", errors="replace")
        elif source.get("url"):
            resp = _build_http_session(source, secrets).get(str(source["url"]), timeout=60)
            resp.raise_for_status()
            text = resp.text
        else:
            text = str(source.get("data") or "")
        for idx, line in enumerate(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict):
                obj.setdefault("id", str(idx))
                items.append(obj)
            else:
                items.append({"id": str(idx), "value": obj})
            if limit is not None and len(items) >= limit:
                break
        return items

    return items


def execute_pipeline(
    raw_spec: str,
    secrets: dict[str, Any] | None = None,
    limit: int | None = None,
) -> list[IngestDocument]:
    spec = parse_pipeline_spec(raw_spec)
    env = create_sandbox_env()
    items = _fetch_source_items(spec.source, secrets or {}, limit=limit)

    doc_id_tpl = env.from_string(spec.transform.doc_id)
    filename_tpl = env.from_string(spec.transform.filename)
    content_tpl = env.from_string(spec.transform.template)
    meta_tpls = {k: env.from_string(v) for k, v in spec.transform.metadata.items()}

    docs: list[IngestDocument] = []
    for idx, item in enumerate(items):
        ctx = {"item": item, "index": idx}
        doc_id = doc_id_tpl.render(**ctx).strip() or f"item-{idx}"
        filename = filename_tpl.render(**ctx).strip() or f"{doc_id}.md"
        raw_bytes = item.get("_raw_bytes")
        if raw_bytes is not None:
            docs.append(
                IngestDocument(
                    doc_id=doc_id,
                    filename=filename,
                    content_type="binary",
                    raw_bytes=raw_bytes,
                    chunking=spec.chunking,
                    metadata={k: tpl.render(**ctx).strip() for k, tpl in meta_tpls.items()},
                )
            )
        else:
            rendered_text = content_tpl.render(**ctx).strip()
            if not rendered_text:
                continue
            docs.append(
                IngestDocument(
                    doc_id=doc_id,
                    filename=filename,
                    content_type="text" if spec.transform.content_type != "binary" else "binary",
                    content=rendered_text,
                    chunking=spec.chunking,
                    metadata={k: tpl.render(**ctx).strip() for k, tpl in meta_tpls.items()},
                )
            )
    return docs


def dry_run_pipeline(
    raw_spec: str,
    secrets: dict[str, Any] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    spec = parse_pipeline_spec(raw_spec)
    docs = execute_pipeline(raw_spec, secrets=secrets, limit=limit)
    previews: list[dict[str, Any]] = []
    for doc in docs:
        chunks = apply_chunking(doc.content, doc.chunking) if doc.content_type == "text" else []
        previews.append(
            {
                "doc_id": doc.doc_id,
                "filename": doc.filename,
                "content_type": doc.content_type,
                "metadata": doc.metadata,
                "chunk_count": len(chunks),
                "content_preview": (doc.content[:400] + "...") if len(doc.content) > 400 else doc.content,
            }
        )
    return {"name": spec.name, "sync_mode": spec.sync_mode, "preview_count": len(previews), "documents": previews}
