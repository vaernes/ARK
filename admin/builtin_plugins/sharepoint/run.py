from __future__ import annotations

from pathlib import Path

import requests
from requests_ntlm import HttpNtlmAuth

from ark_ingest import Document, IngestContext, run

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".html"}


def sync(ctx: IngestContext):
    cfg = ctx.config
    site_url = cfg["site_url"].rstrip("/")
    folder_url = cfg["folder_server_relative_url"]
    username = cfg["username"]
    password = cfg["password"]
    verify_tls = bool(cfg.get("verify_tls", True))

    session = requests.Session()
    session.auth = HttpNtlmAuth(username, password)
    session.verify = verify_tls
    session.headers.update({"Accept": "application/json;odata=verbose"})

    api_url = f"{site_url}/_api/web/GetFolderByServerRelativeUrl('{folder_url}')/Files"
    resp = session.get(api_url, timeout=60)
    resp.raise_for_status()
    files = resp.json().get("d", {}).get("results", [])

    for item in files:
        name = item.get("Name", "")
        if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        srv_rel = item.get("ServerRelativeUrl")
        dl_resp = session.get(f"{site_url}/_api/web/GetFileByServerRelativeUrl('{srv_rel}')/$value", timeout=120)
        dl_resp.raise_for_status()
        yield Document(
            doc_id=str(srv_rel),
            filename=name,
            content=dl_resp.content,
            content_type="binary",
            metadata={"server_relative_url": srv_rel},
        )


if __name__ == "__main__":
    run(sync)
