from __future__ import annotations

from typing import Any

import requests

from ark_ingest import Document, IngestContext, run


def _extract_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def sync(ctx: IngestContext):
    cfg = ctx.config
    headers = {"Accept": "application/json"}
    token = (cfg.get("bearer_token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(cfg["url"], headers=headers, verify=bool(cfg.get("verify_tls", True)), timeout=60)
    resp.raise_for_status()
    data = resp.json()

    items_path = (cfg.get("items_path") or "").strip()
    items = _extract_path(data, items_path) if items_path else data
    if not isinstance(items, list):
        items = [items] if items is not None else []

    id_field = cfg.get("id_field", "id")
    title_field = cfg.get("title_field", "title")
    text_field = cfg.get("text_field", "content")

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        doc_id = str(item.get(id_field) or idx)
        title = str(item.get(title_field) or f"{doc_id}.txt")
        text = str(item.get(text_field) or "")
        if not text.strip():
            continue
        yield Document(
            doc_id=doc_id,
            filename=title,
            content=text,
            content_type="text",
            metadata={k: v for k, v in item.items() if k != text_field and isinstance(v, (str, int, float, bool))},
        )


if __name__ == "__main__":
    run(sync)
