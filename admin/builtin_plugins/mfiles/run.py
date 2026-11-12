from __future__ import annotations

import requests

from ark_ingest import Checkpoint, Document, IngestContext, run

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".html"}


def sync(ctx: IngestContext):
    cfg = ctx.config
    base_url = cfg["base_url"].rstrip("/")
    vault_guid = cfg["vault_guid"]
    username = cfg["username"]
    password = cfg["password"]
    verify_tls = bool(cfg.get("verify_tls", True))
    object_type = int(cfg.get("object_type", 0))
    page_size = min(max(int(cfg.get("page_size", 100) or 100), 1), 200)
    max_items = int(ctx.limit or cfg.get("max_items") or 1000)

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

    emitted = 0
    offset = 0
    while emitted < max_items:
        search_resp = session.get(
            f"{base_url}/REST/objects",
            params={"o": object_type, "limit": page_size, "offset": offset},
            timeout=60,
        )
        search_resp.raise_for_status()
        payload = search_resp.json()
        items = payload.get("Items", [])
        if not items:
            break

        for obj in items:
            obj_ver = obj.get("ObjVer", {})
            obj_type = obj_ver.get("Type")
            obj_id = obj_ver.get("ID")
            ver = obj_ver.get("Version")
            last_modified = obj.get("LastModifiedUtc") or ""
            for f in obj.get("Files", []):
                if emitted >= max_items:
                    break
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
                    metadata={
                        "obj_type": obj_type,
                        "obj_id": obj_id,
                        "version": ver,
                        "last_modified": last_modified,
                    },
                )
                emitted += 1
            if emitted >= max_items:
                break

        offset += len(items)
        if not payload.get("MoreResults") and len(items) < page_size:
            break

    yield Checkpoint({"last_sync": ctx.now_iso(), "emitted": emitted})


if __name__ == "__main__":
    run(sync)
