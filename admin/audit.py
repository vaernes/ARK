"""
ARK Audit Logger: Local PostgreSQL + JSONL file + RFC 5424 Syslog SIEM forwarder.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

logger = logging.getLogger("ark-audit")


class AuditLogger:
    """Persists security/API/admin events locally and optionally forwards to SIEM."""

    def __init__(self, db_dsn: str, log_file: Path) -> None:
        self.db_dsn = db_dsn
        self.log_file = log_file
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.siem_host = os.environ.get("SIEM_SYSLOG_HOST", "").strip()
        self.siem_port = int(os.environ.get("SIEM_SYSLOG_PORT", "514"))
        self.siem_proto = os.environ.get("SIEM_SYSLOG_PROTOCOL", "udp").strip().lower()

    def record(
        self,
        event_type: str,
        actor: str,
        project_slug: str | None = None,
        status: str = "success",
        details: dict[str, Any] | None = None,
    ) -> None:
        ts = datetime.now(timezone.utc)
        payload = details or {}
        entry = {
            "timestamp": ts.isoformat(),
            "event_type": event_type,
            "actor": actor,
            "project_slug": project_slug,
            "status": status,
            "details": payload,
        }

        # 1. Local JSONL append
        try:
            with self.log_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except Exception as exc:
            logger.error("Failed writing audit JSONL: %s", exc)

        # 2. Local PostgreSQL audit_logs table
        try:
            with psycopg.connect(self.db_dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO audit_logs (created_at, event_type, actor, project_slug, status, details)
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                        """,
                        (ts, event_type, actor, project_slug, status, json.dumps(payload, default=str)),
                    )
                conn.commit()
        except Exception as exc:
            logger.error("Failed writing audit event to PostgreSQL: %s", exc)

        # 3. Optional SIEM Syslog forwarding (RFC 5424)
        if self.siem_host:
            self._send_syslog(ts, entry)

    def _send_syslog(self, ts: datetime, entry: dict[str, Any]) -> None:
        pri = 134  # local0.info (16*8 + 6)
        hostname = socket.gethostname()
        msg = f"<{pri}>1 {ts.strftime('%Y-%m-%dT%H:%M:%SZ')} {hostname} ark-admin - - - {json.dumps(entry, default=str)}"
        data = msg.encode("utf-8")
        try:
            if self.siem_proto == "tcp":
                with socket.create_connection((self.siem_host, self.siem_port), timeout=3) as sock:
                    sock.sendall(data + b"\n")
            else:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.sendto(data, (self.siem_host, self.siem_port))
        except Exception as exc:
            logger.warning("Failed forwarding audit event to SIEM (%s:%s): %s", self.siem_host, self.siem_port, exc)
