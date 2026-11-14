from __future__ import annotations

from collections import deque
from pathlib import Path
from urllib.parse import quote

import requests
from requests_ntlm import HttpNtlmAuth

from ark_ingest import Checkpoint, Document, IngestContext, run

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".html"}


def _odata_quote(path_str: str) -> str:
    return quote(path_str.replace("'", "''"), safe="/-_.'")


def sync(ctx: IngestContext):
    cfg = ctx.config
    site_url = cfg["site_url"].rstrip("/")
    folder_url = cfg["folder_server_relative_url"]
    username = cfg["username"]
    password = cfg["password"]
    verify_tls = bool(cfg.get("verify_tls", True))
    recursive = bool(cfg.get("recursive", False))
    max_items = int(ctx.limit or cfg.get("max_items") or 1000)

    session = requests.Session()
    session.auth = HttpNtlmAuth(username, password)
    session.verify = verify_tls
    session.headers.update({"Accept": "application/json;odata=verbose"})

    emitted = 0
    visited_folders: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(folder_url, 0)])

    while queue and emitted < max_items:
        cur_folder, depth = queue.popleft()
        if cur_folder in visited_folders:
            continue
        visited_folders.add(cur_folder)

        next_url: str | None = f"{site_url}/_api/web/GetFolderByServerRelativeUrl('{_odata_quote(cur_folder)}')/Files?$top=200"
        while next_url and emitted < max_items:
            resp = session.get(next_url, timeout=60)
            resp.raise_for_status()
            d_obj = resp.json().get("d", {})
            files = d_obj.get("results", [])

            for item in files:
                if emitted >= max_items:
                    break
                name = item.get("Name", "")
                if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                srv_rel = item.get("ServerRelativeUrl")
                dl_resp = session.get(
                    f"{site_url}/_api/web/GetFileByServerRelativeUrl('{_odata_quote(str(srv_rel))}')/$value",
                    timeout=120,
                )
                dl_resp.raise_for_status()
                yield Document(
                    doc_id=str(srv_rel),
                    filename=name,
                    content=dl_resp.content,
                    content_type="binary",
                    metadata={
                        "server_relative_url": srv_rel,
                        "last_modified": item.get("TimeLastModified", ""),
                    },
                )
                emitted += 1

            next_url = d_obj.get("__next")

        if recursive and depth < 5 and emitted < max_items:
            sub_url: str | None = (
                f"{site_url}/_api/web/GetFolderByServerRelativeUrl('{_odata_quote(cur_folder)}')/Folders?$top=200"
            )
            while sub_url:
                sub_resp = session.get(sub_url, timeout=60)
                sub_resp.raise_for_status()
                sub_d = sub_resp.json().get("d", {})
                for sub in sub_d.get("results", []):
                    sub_rel = sub.get("ServerRelativeUrl")
                    sub_name = str(sub.get("Name") or "")
                    if sub_rel and not sub_name.startswith("_") and sub_name.lower() != "forms":
                        queue.append((str(sub_rel), depth + 1))
                sub_url = sub_d.get("__next")

    yield Checkpoint({"last_sync": ctx.now_iso(), "emitted": emitted})


if __name__ == "__main__":
    run(sync)
