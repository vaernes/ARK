"""Backup path validation, shell-free pg_dump/psql streaming, and manifest verification."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

FORBIDDEN_BACKUP_ROOTS = {
    "/",
    "/app",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib64",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
    "/var",
}

CHUNK_SIZE = 65536


def validate_backup_dir(raw_path: str) -> Path:
    """Validate a configured backup directory path and prevent injection or system-path writes."""
    cleaned = (raw_path or "").strip()
    if not cleaned:
        raise ValueError("Backup directory path cannot be empty.")
    if any(ch in cleaned for ch in ("\x00", "\n", "\r")):
        raise ValueError("Backup directory path contains invalid control characters.")

    raw_p = Path(cleaned)
    if not raw_p.is_absolute():
        raise ValueError("Backup directory must be an absolute path.")
    if ".." in raw_p.parts:
        raise ValueError("Backup directory path must not contain '..' segments.")

    resolved = raw_p.resolve()
    resolved_str = str(resolved)
    if resolved_str in FORBIDDEN_BACKUP_ROOTS:
        raise ValueError(f"Backup directory cannot be a system root directory: {resolved_str}")
    for forbidden in ("/app", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/sbin", "/sys", "/usr"):
        if resolved_str == forbidden or resolved_str.startswith(forbidden + "/"):
            raise ValueError(f"Backup directory cannot reside inside system directory: {forbidden}")

    return resolved


def validate_backup_name(name: str) -> str:
    """Validate a backup bundle directory name (single path component, no traversal)."""
    cleaned = (name or "").strip()
    if not cleaned or cleaned.startswith(".") or "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise ValueError("Invalid backup bundle name.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", cleaned):
        raise ValueError("Backup bundle name contains unsupported characters.")
    return cleaned


def sha256_file(path: Path) -> str:
    """Compute hex SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_gzip_file(path: Path) -> None:
    """Verify gzip archive CRC and framing by reading through the compressed stream."""
    with gzip.open(path, "rb") as gz:
        while gz.read(CHUNK_SIZE):
            pass


def dump_postgres_gzip(
    db_name: str,
    out_file: Path,
    pg_host: str,
    pg_port: str | int,
    pg_user: str,
    pg_password: str,
    pg_dump_bin: str = "pg_dump",
) -> dict[str, Any]:
    """Run pg_dump without a shell and stream stdout directly into a gzip-compressed file."""
    env = os.environ.copy()
    env["PGPASSWORD"] = pg_password

    cmd = [
        pg_dump_bin,
        "-h",
        str(pg_host),
        "-p",
        str(pg_port),
        "-U",
        pg_user,
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        db_name,
    ]

    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    ) as proc:
        assert proc.stdout is not None
        assert proc.stderr is not None

        try:
            with gzip.open(out_file, "wb") as gz_out:
                while True:
                    chunk = proc.stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    gz_out.write(chunk)
            stderr_bytes = proc.stderr.read()
            returncode = proc.wait()
            if returncode != 0:
                err_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"pg_dump failed for database '{db_name}' (exit {returncode}): {err_msg}")
        except Exception:
            out_file.unlink(missing_ok=True)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            raise

    return {
        "database": db_name,
        "file": out_file.name,
        "bytes": out_file.stat().st_size,
        "sha256": sha256_file(out_file),
    }


def restore_postgres_gzip(
    db_name: str,
    dump_file: Path,
    pg_host: str,
    pg_port: str | int,
    pg_user: str,
    pg_password: str,
    psql_bin: str = "psql",
) -> None:
    """Decompress a .sql.gz dump and stream it into psql without a shell."""
    env = os.environ.copy()
    env["PGPASSWORD"] = pg_password

    cmd = [
        psql_bin,
        "-h",
        str(pg_host),
        "-p",
        str(pg_port),
        "-U",
        pg_user,
        "-d",
        db_name,
        "-q",
        "-v",
        "ON_ERROR_STOP=1",
    ]

    with subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    ) as proc:
        assert proc.stdin is not None
        assert proc.stderr is not None

        try:
            with gzip.open(dump_file, "rb") as gz_in:
                while True:
                    chunk = gz_in.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
            proc.stdin.flush()
            proc.stdin.close()
            stderr_bytes = proc.stderr.read()
            returncode = proc.wait()
            if returncode != 0:
                err_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"psql restore failed for database '{db_name}' (exit {returncode}): {err_msg}"
                )
        except Exception:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            raise


def verify_backup_manifest(backup_dir: Path) -> dict[str, Any]:
    """Verify backup directory integrity (manifest SHA-256 checksums + gzip framing)."""
    root = backup_dir.resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Backup manifest not found: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for db_entry in manifest.get("databases", []):
        rel_file = db_entry.get("file", "")
        target = (root / rel_file).resolve()
        if root not in target.parents or not target.is_file():
            raise FileNotFoundError(f"Missing database dump in backup: {rel_file}")
        expected_sha = db_entry.get("sha256", "")
        actual_sha = sha256_file(target)
        if expected_sha and actual_sha != expected_sha:
            raise ValueError(f"SHA-256 mismatch for {rel_file}: expected {expected_sha}, got {actual_sha}")
        verify_gzip_file(target)

    for snap_entry in manifest.get("qdrant_snapshots", []):
        rel_path = snap_entry.get("path", "")
        target = (root / rel_path).resolve()
        if root not in target.parents or not target.is_file():
            raise FileNotFoundError(f"Missing Qdrant snapshot in backup: {rel_path}")
        expected_sha = snap_entry.get("sha256", "")
        actual_sha = sha256_file(target)
        if expected_sha and actual_sha != expected_sha:
            raise ValueError(f"SHA-256 mismatch for {rel_path}: expected {expected_sha}, got {actual_sha}")

    extra_archives = manifest.get("extra_archives") or {}
    if isinstance(extra_archives, dict):
        for archive_key, entry in extra_archives.items():
            if not isinstance(entry, dict):
                continue
            rel_file = entry.get("file", "")
            if not rel_file:
                continue
            target = (root / rel_file).resolve()
            if root not in target.parents or not target.is_file():
                raise FileNotFoundError(f"Missing extra archive '{archive_key}' in backup: {rel_file}")
            expected_sha = entry.get("sha256", "")
            actual_sha = sha256_file(target)
            if expected_sha and actual_sha != expected_sha:
                raise ValueError(f"SHA-256 mismatch for {rel_file}: expected {expected_sha}, got {actual_sha}")

    return manifest
