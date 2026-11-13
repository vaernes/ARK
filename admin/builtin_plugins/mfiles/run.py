from __future__ import annotations

import requests

from ark_ingest import Document, IngestContext, run

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".html"}


def sync(ctx: IngestContext):
    cfg = ctx.config
    base_url = cfg["base_url"].rstrip("/")
    vault_guid = cfg["vault_guid"]
    username = cfg["username"]
    password = cfg["password"]
    verify_tls = bool(cfg.get("verify_tls", True))
    object_type = int(cfg.get("object_type", 0))

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
            yield Document(
                doc_id=f"{obj_type}/{obj_id}/{file_id}",
                filename=filename,
                content=dl_resp.content,
                content_type="binary",
                metadata={"obj_type": obj_type, "obj_id": obj_id, "version": ver},
            )


if __name__ == "__main__":
    run(sync)
