#!/usr/bin/env python3
"""
ARK Active Directory -> Open WebUI RBAC Group Synchronizer.

Queries on-prem Active Directory security groups (default prefix: 'ARK-')
via LDAPS and idempotently synchronizes their member lists into matching
Open WebUI RBAC groups (e.g. 'ARK-Project-Alpha' -> 'project-alpha').
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import ssl
import sys
from pathlib import Path
from typing import Any

import ldap3
import requests
import urllib3
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("ark-ad-sync")


class ADSyncConfig(BaseModel):
    ldap_host: str = Field(min_length=1)
    ldap_port: int = Field(default=636, gt=0)
    ldap_use_tls: bool = True
    ldap_validate_cert: bool = True
    ldap_ca_cert_file: str | None = None
    ldap_app_dn: str = Field(min_length=1)
    ldap_app_password: str = Field(min_length=1)
    ldap_search_base: str = Field(min_length=1)
    ad_group_prefix: str = Field(default="ARK-")
    webui_url: str = Field(default="https://ark.local")
    webui_api_key: str = Field(min_length=1)
    verify_webui_tls: bool = True
    dry_run: bool = False


def load_env_file(env_path: Path) -> None:
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def normalize_group_name(ad_cn: str, prefix: str) -> str:
    """Convert 'ARK-Project_Alpha' -> 'project-alpha'."""
    name = ad_cn[len(prefix):] if prefix and ad_cn.startswith(prefix) else ad_cn
    tokens = [t.lower() for t in re.split(r"[^A-Za-z0-9]+", name) if t]
    return "-".join(tokens)


def connect_ad(cfg: ADSyncConfig) -> ldap3.Connection:
    tls = None
    if cfg.ldap_use_tls:
        cert_reqs = ssl.CERT_REQUIRED if cfg.ldap_validate_cert else ssl.CERT_NONE
        tls = ldap3.Tls(
            validate=cert_reqs,
            ca_certs_file=cfg.ldap_ca_cert_file if cfg.ldap_validate_cert else None,
        )
    server = ldap3.Server(
        cfg.ldap_host,
        port=cfg.ldap_port,
        use_ssl=(cfg.ldap_port == 636 and cfg.ldap_use_tls),
        tls=tls,
        get_info=ldap3.NONE,
    )
    conn = ldap3.Connection(
        server,
        user=cfg.ldap_app_dn,
        password=cfg.ldap_app_password,
        auto_bind=True,
    )
    if cfg.ldap_use_tls and cfg.ldap_port != 636:
        conn.start_tls()
    return conn


def get_ad_group_member_emails(
    conn: ldap3.Connection,
    search_base: str,
    group_dn: str,
) -> set[str]:
    """Resolve direct and nested AD group members' lowercase email addresses."""
    # 1.2.840.113556.1.4.1941 = LDAP_MATCHING_RULE_IN_CHAIN (transitive membership)
    escaped_dn = ldap3.utils.conv.escape_filter_chars(group_dn)
    user_filter = (
        f"(&(objectClass=user)(objectCategory=person)"
        f"(!(userAccountControl:1.2.840.113556.1.4.803:=2))"
        f"(memberOf:1.2.840.113556.1.4.1941:={escaped_dn}))"
    )
    conn.search(
        search_base=search_base,
        search_filter=user_filter,
        attributes=["mail", "sAMAccountName"],
    )
    emails: set[str] = set()
    for entry in conn.entries:
        mail_val = str(entry.mail.value).strip().lower() if entry.mail.value else ""
        if mail_val:
            emails.add(mail_val)
    return emails


def sync_groups(cfg: ADSyncConfig) -> None:
    session = requests.Session()
    session.verify = cfg.verify_webui_tls
    session.headers.update(
        {
            "Authorization": f"Bearer {cfg.webui_api_key}",
            "Content-Type": "application/json",
        }
    )
    if not cfg.verify_webui_tls:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    base = cfg.webui_url.rstrip("/")
    users_resp = session.get(f"{base}/api/v1/users/", timeout=30)
    users_resp.raise_for_status()
    webui_users = users_resp.json()
    email_to_uid: dict[str, str] = {
        str(u.get("email", "")).strip().lower(): u["id"]
        for u in webui_users
        if u.get("email") and u.get("id")
    }

    groups_resp = session.get(f"{base}/api/v1/groups/", timeout=30)
    groups_resp.raise_for_status()
    webui_groups: dict[str, dict[str, Any]] = {
        g["name"]: g for g in groups_resp.json() if g.get("name")
    }

    conn = connect_ad(cfg)
    try:
        prefix_filter = ldap3.utils.conv.escape_filter_chars(cfg.ad_group_prefix)
        conn.search(
            search_base=cfg.ldap_search_base,
            search_filter=f"(&(objectClass=group)(cn={prefix_filter}*))",
            attributes=["cn", "distinguishedName"],
        )
        ad_groups = list(conn.entries)
        logger.info("Found %d AD group(s) matching prefix '%s'.", len(ad_groups), cfg.ad_group_prefix)

        for ad_grp in ad_groups:
            ad_cn = str(ad_grp.cn.value)
            ad_dn = str(ad_grp.distinguishedName.value)
            target_name = normalize_group_name(ad_cn, cfg.ad_group_prefix)

            if target_name not in webui_groups:
                logger.warning(
                    "AD group '%s' -> '%s' has no matching Open WebUI group; skipping "
                    "(run bootstrap_ark.py first).",
                    ad_cn,
                    target_name,
                )
                continue

            member_emails = get_ad_group_member_emails(conn, cfg.ldap_search_base, ad_dn)
            desired_uids = sorted(
                email_to_uid[email] for email in member_emails if email in email_to_uid
            )
            current_grp = webui_groups[target_name]
            current_uids = sorted(current_grp.get("user_ids") or [])

            if desired_uids == current_uids:
                logger.info("Group '%s' is already in sync (%d user(s)).", target_name, len(desired_uids))
                continue

            if cfg.dry_run:
                logger.info(
                    "[DRY-RUN] Would update '%s' from %d to %d user(s).",
                    target_name,
                    len(current_uids),
                    len(desired_uids),
                )
                continue

            update_resp = session.post(
                f"{base}/api/v1/groups/id/{current_grp['id']}/update",
                json={
                    "name": current_grp["name"],
                    "description": current_grp.get("description", ""),
                    "permissions": current_grp.get("permissions", {}),
                    "user_ids": desired_uids,
                },
                timeout=30,
            )
            update_resp.raise_for_status()
            logger.info(
                "Synchronized group '%s' (%d -> %d user(s)).",
                target_name,
                len(current_uids),
                len(desired_uids),
            )
    finally:
        conn.unbind()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync Active Directory security groups to Open WebUI RBAC groups."
    )
    parser.add_argument(
        "--env-file",
        default=str(Path(__file__).resolve().parent.parent / ".env"),
        help="Path to .env file.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report changes without applying.")
    parser.add_argument("--insecure", action="store_true", help="Skip TLS verification for Open WebUI.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parent.parent
    load_env_file(Path(args.env_file))

    ca_cert_env = os.environ.get("LDAP_CA_CERT_FILE", "/certs/ad-ca.crt")
    # If running on host and container path '/certs/...' isn't present, map to ./certs/
    if ca_cert_env.startswith("/certs/") and not Path(ca_cert_env).exists():
        host_ca = repo_root / "certs" / Path(ca_cert_env).name
        if host_ca.exists():
            ca_cert_env = str(host_ca)

    try:
        cfg = ADSyncConfig(
            ldap_host=os.environ.get("LDAP_SERVER_HOST", ""),
            ldap_port=int(os.environ.get("LDAP_SERVER_PORT", "636")),
            ldap_use_tls=os.environ.get("LDAP_USE_TLS", "true").lower() == "true",
            ldap_validate_cert=os.environ.get("LDAP_VALIDATE_CERT", "true").lower() == "true",
            ldap_ca_cert_file=ca_cert_env,
            ldap_app_dn=os.environ.get("LDAP_APP_DN", ""),
            ldap_app_password=os.environ.get("LDAP_APP_PASSWORD", ""),
            ldap_search_base=os.environ.get("LDAP_SEARCH_BASE", ""),
            ad_group_prefix=os.environ.get("AD_GROUP_PREFIX", "ARK-"),
            webui_url=os.environ.get("WEBUI_URL", "https://ark.local"),
            webui_api_key=os.environ.get("OPENWEBUI_API_KEY", ""),
            verify_webui_tls=not args.insecure,
            dry_run=args.dry_run,
        )
        sync_groups(cfg)
    except Exception as exc:
        logger.error("AD group sync failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
