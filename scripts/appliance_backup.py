#!/usr/bin/env python3
"""Create and restore encrypted cold backups of an ARK appliance."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import struct
import sys
import tarfile
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.base import CipherContext

MAGIC = b"ARKDR1\n"
CHUNK_SIZE = 1024 * 1024
STATE_PATHS = (
    "VERSION", "versions.env", "docker-compose.yml", "docker-compose.llm.yml", "docker-compose.test.yml",
    "admin", "llm-admin", "provisioning", "scripts", "plugins", "data/postgres", "data/qdrant",
    "data/open-webui", "data/audit", "data/ingest-scripts", "artifacts/models", "artifacts/snapshots",
)
SECRET_PATHS = (".env", "certs", "config", "data/caddy")
REQUIRED_PATHS = {"VERSION", "docker-compose.yml", "data/postgres", "data/qdrant", "artifacts/models", ".env"}


def load_public_key(path: Path) -> rsa.RSAPublicKey:
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
        raise ValueError("Recovery public keys must be RSA keys of at least 3072 bits.")
    return key


def load_private_key(path: Path) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 3072:
        raise ValueError("Recovery private keys must be RSA keys of at least 3072 bits.")
    return key


def key_fingerprint(key: rsa.RSAPublicKey) -> str:
    encoded = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(encoded).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


class EncryptedWriter:
    def __init__(self, stream: BinaryIO, encryptor: CipherContext) -> None:
        self.stream = stream
        self.encryptor = encryptor

    def write(self, data: bytes) -> int:
        self.stream.write(self.encryptor.update(data))
        return len(data)

    def flush(self) -> None:
        self.stream.flush()


def safe_member(info: tarfile.TarInfo) -> tarfile.TarInfo:
    path = PurePosixPath(info.name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"Unsafe archive path: {info.name}")
    if not (info.isdir() or info.isfile()):
        raise ValueError(f"Links and special files cannot be backed up or restored: {info.name}")
    return info


def write_encrypted_archive(
    destination: Path, root: Path, paths: tuple[str, ...], key: rsa.RSAPublicKey,
    backup_id: str, kind: str, images: Path | None = None,
) -> None:
    content_key = os.urandom(32)
    nonce = os.urandom(12)
    wrapped = key.encrypt(content_key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                  algorithm=hashes.SHA256(), label=None))
    header = json.dumps({
        "backup_id": backup_id, "kind": kind, "nonce": base64.b64encode(nonce).decode("ascii"),
        "wrapped_key": base64.b64encode(wrapped).decode("ascii"),
        "key_fingerprint": key_fingerprint(key),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encryptor = Cipher(algorithms.AES(content_key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    with destination.open("xb") as stream:
        os.chmod(destination, 0o600)
        stream.write(MAGIC + struct.pack(">I", len(header)) + header)
        with tarfile.open(fileobj=EncryptedWriter(stream, encryptor), mode="w|gz") as archive:
            for name in paths:
                source = root / name
                if source.is_symlink():
                    raise ValueError(f"Appliance path must not be a symlink: {source}")
                if not source.exists():
                    if name in REQUIRED_PATHS:
                        raise FileNotFoundError(f"Required appliance path is missing: {source}")
                    continue
                if root not in source.resolve().parents:
                    raise ValueError(f"Appliance path must not be a symlink or escape the root: {source}")
                archive.add(source, arcname=name, recursive=True, filter=safe_member)
            if images is not None:
                if images.is_symlink() or not images.is_file():
                    raise ValueError("Docker image archive must be a regular file.")
                archive.add(images, arcname="artifacts/ark-recovery-images.tar.zst", filter=safe_member)
        stream.write(encryptor.finalize())
        stream.write(encryptor.tag)


def decrypt_archive(source: Path, destination: Path, key: rsa.RSAPrivateKey, backup_id: str, kind: str) -> None:
    with source.open("rb") as stream:
        if stream.read(len(MAGIC)) != MAGIC:
            raise ValueError(f"Invalid encrypted archive: {source.name}")
        size_bytes = stream.read(4)
        if len(size_bytes) != 4:
            raise ValueError("Truncated encrypted archive header.")
        header_len = struct.unpack(">I", size_bytes)[0]
        if header_len < 1 or header_len > 16384:
            raise ValueError("Invalid encrypted archive header length.")
        header = stream.read(header_len)
        if len(header) != header_len:
            raise ValueError("Truncated encrypted archive header.")
        metadata = json.loads(header)
        if (metadata.get("backup_id") != backup_id or metadata.get("kind") != kind
                or metadata.get("key_fingerprint") != key_fingerprint(key.public_key())):
            raise ValueError(f"Wrong recovery key or archive identity for {kind}.")
        nonce = base64.b64decode(metadata["nonce"], validate=True)
        wrapped = base64.b64decode(metadata["wrapped_key"], validate=True)
        content_key = key.decrypt(wrapped, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                        algorithm=hashes.SHA256(), label=None))
        ciphertext_start = stream.tell()
        stream.seek(0, os.SEEK_END)
        remaining = stream.tell() - ciphertext_start - 16
        if remaining < 0:
            raise ValueError("Truncated encrypted archive.")
        stream.seek(-16, os.SEEK_END)
        tag = stream.read(16)
        stream.seek(ciphertext_start)
        decryptor = Cipher(algorithms.AES(content_key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        with destination.open("xb") as output:
            os.chmod(destination, 0o600)
            while remaining:
                chunk = stream.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise ValueError("Truncated encrypted archive.")
                output.write(decryptor.update(chunk))
                remaining -= len(chunk)
            output.write(decryptor.finalize())


def extract_archive(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            safe_member(member)
            member.mode &= 0o777
        archive.extractall(destination, members=members, numeric_owner=True, filter="fully_trusted")


def create(
    root: Path, output: Path, data_key: Path, secrets_key: Path, images: Path, signing_key: Path,
    owner: tuple[int, int] | None = None,
) -> None:
    root = root.resolve(strict=True)
    data_public = load_public_key(data_key)
    secrets_public = load_public_key(secrets_key)
    if key_fingerprint(data_public) == key_fingerprint(secrets_public):
        raise ValueError("Data and secret escrow must use different recovery keys.")
    signer = serialization.load_pem_private_key(signing_key.read_bytes(), password=None)
    if not isinstance(signer, ed25519.Ed25519PrivateKey) or signing_key.stat().st_mode & 0o077:
        raise ValueError("Backup signing requires an owner-only Ed25519 private key.")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    backup_id = uuid.uuid4().hex
    try:
        state = output / "state.ark"
        secrets = output / "secrets.ark"
        write_encrypted_archive(state, root, STATE_PATHS, data_public, backup_id, "state", images)
        write_encrypted_archive(secrets, root, SECRET_PATHS, secrets_public, backup_id, "secrets")
        manifest = {
            "format": "ark-appliance-backup/v1",
            "backup_id": backup_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ark_version": (root / "VERSION").read_text(encoding="utf-8").strip(),
            "archives": {
                "state": {"file": state.name, "sha256": file_hash(state)},
                "secrets": {"file": secrets.name, "sha256": file_hash(secrets)},
            },
        }
        manifest_path = output / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        signature_path = output / "manifest.json.sig"
        signature_path.write_bytes(signer.sign(manifest_path.read_bytes()))
        os.chmod(signature_path, 0o600)
        if owner is not None:
            for path in (state, secrets, manifest_path, signature_path, output):
                os.chown(path, *owner)
    except Exception:
        shutil.rmtree(output)
        raise


def recover(
    backup: Path, data_key: Path, secrets_key: Path, verify_key: Path, target: Path | None,
    owner: tuple[int, int] | None = None, work_dir: Path | None = None,
) -> None:
    verifier = serialization.load_pem_public_key(verify_key.read_bytes())
    if not isinstance(verifier, ed25519.Ed25519PublicKey):
        raise ValueError("Backup verification requires an Ed25519 public key.")
    manifest_bytes = (backup / "manifest.json").read_bytes()
    verifier.verify((backup / "manifest.json.sig").read_bytes(), manifest_bytes)
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or manifest.get("format") != "ark-appliance-backup/v1":
        raise ValueError("Invalid appliance backup manifest.")
    backup_id = manifest.get("backup_id")
    if not isinstance(backup_id, str) or not re_full_hex(backup_id):
        raise ValueError("Invalid appliance backup identifier.")
    archives = manifest.get("archives")
    if not isinstance(archives, dict):
        raise ValueError("Appliance backup archive inventory is missing.")
    keys = {"state": load_private_key(data_key), "secrets": load_private_key(secrets_key)}
    if key_fingerprint(keys["state"].public_key()) == key_fingerprint(keys["secrets"].public_key()):
        raise ValueError("Data and secret escrow must use different recovery keys.")
    if target is not None and target.exists():
        raise ValueError("Recovery target must not already exist.")
    temp_parent = work_dir or (target.parent if target is not None else None)
    if target is not None and work_dir is not None and os.stat(work_dir).st_dev != os.stat(target.parent).st_dev:
        raise ValueError("Recovery work directory and target must be on the same filesystem.")
    with tempfile.TemporaryDirectory(prefix="ark-recovery-", dir=temp_parent) as tmp:
        temp = Path(tmp)
        decrypted: dict[str, Path] = {}
        for kind in ("state", "secrets"):
            entry = archives.get(kind)
            if not isinstance(entry, dict) or entry.get("file") != f"{kind}.ark":
                raise ValueError(f"Missing {kind} archive in manifest.")
            source = backup / entry["file"]
            if source.is_symlink() or not source.is_file() or file_hash(source) != entry.get("sha256"):
                raise ValueError(f"Checksum mismatch or missing {kind} archive.")
            decrypted[kind] = temp / f"{kind}.tar.gz"
            decrypt_archive(source, decrypted[kind], keys[kind], backup_id, kind)
            with tarfile.open(decrypted[kind], "r:gz") as archive:
                names: set[str] = set()
                for member in archive:
                    safe_member(member)
                    names.add(member.name.rstrip("/"))
                required = (REQUIRED_PATHS.intersection(STATE_PATHS) | {"artifacts/ark-recovery-images.tar.zst"}
                            if kind == "state" else REQUIRED_PATHS.intersection(SECRET_PATHS))
                if not required.issubset(names):
                    raise ValueError(f"Incomplete {kind} archive: missing {sorted(required - names)}.")
        if target is not None:
            staging = temp / "restored"
            staging.mkdir(mode=0o700)
            for kind in ("state", "secrets"):
                extract_archive(decrypted[kind], staging)
            if (staging / "VERSION").read_text(encoding="utf-8").strip() != manifest.get("ark_version"):
                raise ValueError("Restored application version does not match the backup manifest.")
            if owner is not None:
                os.chown(staging, *owner)
                os.chown(staging / ".env", *owner)
                os.chmod(staging / ".env", 0o600)
            os.replace(staging, target)


def re_full_hex(value: str) -> bool:
    return len(value) == 32 and all(char in "0123456789abcdef" for char in value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "verify", "restore"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--data-key", type=Path, required=True)
    parser.add_argument("--secrets-key", type=Path, required=True)
    parser.add_argument("--images", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--signing-key", type=Path)
    parser.add_argument("--verify-key", type=Path)
    parser.add_argument("--owner-uid", type=int)
    parser.add_argument("--owner-gid", type=int)
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    try:
        if (args.owner_uid is None) != (args.owner_gid is None):
            raise ValueError("Both --owner-uid and --owner-gid are required when setting ownership.")
        owner = None
        if args.owner_uid is not None and args.owner_gid is not None:
            if args.owner_uid < 0 or args.owner_gid < 0:
                raise ValueError("Owner identifiers must be non-negative.")
            owner = (args.owner_uid, args.owner_gid)
        if args.action == "create":
            if args.root is None or args.images is None or args.signing_key is None:
                raise ValueError("Creating a backup requires --root, --images, and --signing-key.")
            create(args.root, args.backup, args.data_key, args.secrets_key, args.images, args.signing_key, owner)
        else:
            if args.verify_key is None or (args.action == "restore" and args.target is None):
                raise ValueError("Recovery requires --verify-key and, for restore, --target.")
            recover(args.backup, args.data_key, args.secrets_key, args.verify_key,
                    args.target if args.action == "restore" else None, owner, args.work_dir)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, tarfile.TarError,
            InvalidTag, InvalidSignature) as exc:
        print(f"ERROR: appliance backup {args.action} failed: {exc}", file=sys.stderr)
        return 1
    print(f"Appliance backup {args.action} succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
