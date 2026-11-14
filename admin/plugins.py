"""
ARK Offline Connector Plugin & Script Runner.

Discovers manifest-declared plugins from `admin/builtin_plugins/` and
`ARK_PLUGINS_DIR` (`/plugins`), plus custom sysadmin scripts in
`ARK_INGEST_SCRIPTS_DIR` (`/ingest-scripts`).

All plugins and custom scripts execute in an isolated, environment-scrubbed
subprocess (or external HTTP sidecar) speaking the NDJSON ARK Document IR
protocol, ensuring they never access `ARK_ADMIN_DB_URL`, `QDRANT_API_KEY`,
`ARK_PROXY_AUTH_SECRET`, or other projects' collections.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from ingest import IngestDocument

logger = logging.getLogger("ark-plugins")

APP_DIR = Path(__file__).resolve().parent
BUILTIN_PLUGINS_DIR = APP_DIR / "builtin_plugins"
SUPPORTED_RUNTIMES = {"python3", "exec", "http"}

SAFE_ENV_KEYS = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "DO_NOT_TRACK",
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
    "OFFLINE_MODE",
    "ARK_ALLOWED_INGEST_PATHS",
)


@dataclass
class PluginManifest:
    id: str
    name: str
    version: str
    ark_protocol: str
    runtime: str
    entrypoint: str
    description: str
    builtin: bool
    supports_incremental: bool
    secret_fields: list[str]
    config_schema: dict[str, Any]
    root_dir: Path

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "ark_protocol": self.ark_protocol,
            "runtime": self.runtime,
            "description": self.description,
            "builtin": self.builtin,
            "supports_incremental": self.supports_incremental,
            "secret_fields": self.secret_fields,
            "config_schema": self.config_schema,
        }


def validate_plugin_manifest(manifest_data: dict[str, Any], root_dir: Path) -> PluginManifest:
    pid = str(manifest_data.get("id") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", pid):
        raise ValueError(f"Invalid plugin id '{pid}' in {root_dir}")

    protocol = str(manifest_data.get("ark_protocol") or "")
    if protocol != "1":
        raise ValueError(f"Unsupported ark_protocol '{protocol}' in plugin '{pid}'")

    runtime = str(manifest_data.get("runtime") or "").strip().lower()
    if runtime not in SUPPORTED_RUNTIMES:
        raise ValueError(f"Unsupported runtime '{runtime}' in plugin '{pid}'")

    entrypoint = str(manifest_data.get("entrypoint") or "").strip()
    if not entrypoint:
        raise ValueError(f"Missing entrypoint in plugin '{pid}'")

    if runtime in {"python3", "exec"}:
        resolved = (root_dir / entrypoint).resolve()
        root_resolved = root_dir.resolve()
        if root_resolved not in resolved.parents and resolved != root_resolved:
            raise ValueError(f"Entrypoint path escapes plugin directory in '{pid}': {entrypoint}")
        if not resolved.is_file():
            raise ValueError(f"Entrypoint file not found in '{pid}': {entrypoint}")
    elif runtime == "http":
        if not (entrypoint.startswith("http://") or entrypoint.startswith("https://")):
            raise ValueError(f"HTTP plugin '{pid}' entrypoint must be http:// or https://")

    return PluginManifest(
        id=pid,
        name=str(manifest_data.get("name") or pid),
        version=str(manifest_data.get("version") or "1.0.0"),
        ark_protocol=protocol,
        runtime=runtime,
        entrypoint=entrypoint,
        description=str(manifest_data.get("description") or ""),
        builtin=bool(manifest_data.get("builtin", False)),
        supports_incremental=bool(manifest_data.get("supports_incremental", False)),
        secret_fields=[str(f) for f in (manifest_data.get("secret_fields") or [])],
        config_schema=dict(manifest_data.get("config_schema") or {"type": "object", "properties": {}}),
        root_dir=root_dir,
    )


def discover_plugins(extra_dir: Path | None = None) -> dict[str, PluginManifest]:
    """Discover built-in and mounted offline plugins."""
    plugins: dict[str, PluginManifest] = {}
    search_dirs = [BUILTIN_PLUGINS_DIR]
    plugins_dir = extra_dir if extra_dir is not None else Path(os.environ.get("ARK_PLUGINS_DIR", "/plugins"))
    if plugins_dir.is_dir():
        search_dirs.append(plugins_dir)

    for base in search_dirs:
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            manifest_path = entry / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                if base != BUILTIN_PLUGINS_DIR:
                    raw["builtin"] = False
                manifest = validate_plugin_manifest(raw, entry)
                plugins[manifest.id] = manifest
            except Exception as exc:
                logger.warning("Skipping invalid plugin in %s: %s", entry, exc)
    return plugins


def list_ingest_scripts(scripts_dir: Path | None = None) -> list[str]:
    root = scripts_dir if scripts_dir is not None else Path(os.environ.get("ARK_INGEST_SCRIPTS_DIR", "/ingest-scripts"))
    if not root.is_dir():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_file() and p.suffix == ".py" and not p.name.startswith("_") and not p.name.startswith(".")
    )


def resolve_ingest_script(script_name: str, scripts_dir: Path | None = None) -> Path:
    cleaned = (script_name or "").strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or ".." in cleaned or not cleaned.endswith(".py"):
        raise ValueError(f"Invalid script filename: {script_name!r}")
    root = (scripts_dir if scripts_dir is not None else Path(os.environ.get("ARK_INGEST_SCRIPTS_DIR", "/ingest-scripts"))).resolve()
    target = (root / cleaned).resolve()
    if root not in target.parents or not target.is_file():
        raise FileNotFoundError(f"Ingest script not found: {cleaned}")
    return target


def build_scrubbed_env(plugin_dir: Path | None = None, base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build a minimal subprocess environment with all ARK control-plane/DB secrets stripped."""
    src = os.environ if base_env is None else base_env
    env: dict[str, str] = {k: src[k] for k in SAFE_ENV_KEYS if k in src}
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["DO_NOT_TRACK"] = "1"
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"

    py_paths = [str(APP_DIR)]
    if plugin_dir is not None:
        py_paths.append(str(plugin_dir))
        wheels_dir = plugin_dir / "wheels"
        if wheels_dir.is_dir():
            py_paths.append(str(wheels_dir))
    env["PYTHONPATH"] = os.pathsep.join(py_paths)
    return env


def _parse_ndjson_lines(
    lines: list[str],
    limit: int | None = None,
) -> tuple[list[IngestDocument], dict[str, Any] | None, list[str]]:
    docs: list[IngestDocument] = []
    checkpoint: dict[str, Any] | None = None
    logs: list[str] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        obj = json.loads(line)
        rec_type = obj.get("type", "document")
        if rec_type == "document":
            docs.append(IngestDocument.model_validate(obj))
            if limit is not None and len(docs) >= limit:
                break
        elif rec_type == "checkpoint":
            cur = obj.get("cursor")
            if isinstance(cur, dict):
                checkpoint = cur
        elif rec_type == "log":
            logs.append(f"[{obj.get('level', 'info')}] {obj.get('message', '')}")
        else:
            raise ValueError(f"Unknown NDJSON record type: {rec_type!r}")
    return docs, checkpoint, logs


def execute_subprocess_ir(
    *,
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    config: dict[str, Any],
    cursor: dict[str, Any] | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    timeout_sec: int | None = None,
) -> tuple[list[IngestDocument], dict[str, Any] | None, list[str]]:
    timeout = timeout_sec or int(os.environ.get("ARK_INGEST_JOB_TIMEOUT_SEC", "1800"))
    stdin_payload = json.dumps(
        {
            "config": config,
            "cursor": cursor or {},
            "dry_run": dry_run,
            "limit": limit,
        },
        default=str,
    )
    proc = subprocess.run(
        cmd,
        input=stdin_payload,
        text=True,
        capture_output=True,
        cwd=str(cwd),
        env=env,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        err_tail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        raise RuntimeError(f"Plugin/script exited with code {proc.returncode}: {err_tail}")

    docs, new_cursor, logs = _parse_ndjson_lines(proc.stdout.splitlines(), limit=limit)
    if proc.stderr.strip():
        logs.extend(proc.stderr.strip().splitlines()[-20:])
    return docs, new_cursor, logs


def execute_plugin(
    manifest: PluginManifest,
    config: dict[str, Any],
    cursor: dict[str, Any] | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> tuple[list[IngestDocument], dict[str, Any] | None, list[str]]:
    if manifest.runtime == "http":
        resp = requests.post(
            f"{manifest.entrypoint.rstrip('/')}/sync",
            json={"config": config, "cursor": cursor or {}, "dry_run": dry_run, "limit": limit},
            timeout=int(os.environ.get("ARK_INGEST_JOB_TIMEOUT_SEC", "1800")),
        )
        resp.raise_for_status()
        return _parse_ndjson_lines(resp.text.splitlines(), limit=limit)

    entry_path = (manifest.root_dir / manifest.entrypoint).resolve()
    cmd = [sys.executable, str(entry_path)] if manifest.runtime == "python3" else [str(entry_path)]
    env = build_scrubbed_env(plugin_dir=manifest.root_dir)
    return execute_subprocess_ir(
        cmd=cmd,
        cwd=manifest.root_dir,
        env=env,
        config=config,
        cursor=cursor,
        dry_run=dry_run,
        limit=limit,
    )


def execute_custom_script(
    script_name: str,
    config: dict[str, Any],
    cursor: dict[str, Any] | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    scripts_dir: Path | None = None,
) -> tuple[list[IngestDocument], dict[str, Any] | None, list[str]]:
    script_path = resolve_ingest_script(script_name, scripts_dir=scripts_dir)
    env = build_scrubbed_env(plugin_dir=script_path.parent)
    return execute_subprocess_ir(
        cmd=[sys.executable, str(script_path)],
        cwd=script_path.parent,
        env=env,
        config=config,
        cursor=cursor,
        dry_run=dry_run,
        limit=limit,
    )
