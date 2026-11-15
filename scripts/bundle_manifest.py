#!/usr/bin/env python3
"""Sign and verify an offline bundle using an independently trusted Ed25519 key."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def file_metadata(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Bundle artifact is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"name": path.name, "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def openssl(*args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["openssl", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError(f"OpenSSL failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def check_key(path: Path, public: bool) -> None:
    if not path.is_file():
        raise ValueError(f"Signing key not found: {path}")
    args = ["pkey"]
    if public:
        args.append("-pubin")
    args.extend(["-in", str(path), "-text", "-noout"])
    if "ED25519" not in openssl(*args).stdout:
        raise ValueError("Bundle signing requires an Ed25519 key.")
    if not public and os.stat(path).st_mode & 0o077:
        raise ValueError("Private signing key must not be accessible to group or other users.")


def sign(manifest_path: Path, key: Path, artifacts: dict[str, Path]) -> None:
    check_key(key, public=False)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Bundle manifest must be an object.")
    manifest["artifacts"] = {name: file_metadata(path) for name, path in artifacts.items()}
    manifest["signature_algorithm"] = "Ed25519"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    signature = manifest_path.with_name(manifest_path.name + ".sig")
    openssl("pkeyutl", "-sign", "-rawin", "-inkey", str(key), "-in", str(manifest_path), "-out", str(signature))


def verify(manifest_path: Path, key: Path, artifacts: dict[str, Path], version: str) -> None:
    check_key(key, public=True)
    signature = manifest_path.with_name(manifest_path.name + ".sig")
    if not signature.is_file():
        raise ValueError("Bundle signature is missing.")
    openssl("pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", str(key),
            "-in", str(manifest_path), "-sigfile", str(signature))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("signature_algorithm") != "Ed25519":
        raise ValueError("Invalid signed bundle manifest.")
    if manifest.get("ark_version") != version:
        raise ValueError("Signed bundle version does not match the installer.")
    signed_artifacts = manifest.get("artifacts")
    if not isinstance(signed_artifacts, dict):
        raise ValueError("Signed bundle has no artifact inventory.")
    for name, path in artifacts.items():
        expected = signed_artifacts.get(name)
        if not isinstance(expected, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(expected.get("sha256", ""))):
            raise ValueError(f"Signed manifest is missing artifact '{name}'.")
        actual = file_metadata(path)
        if actual["sha256"] != expected["sha256"] or actual["bytes"] != expected.get("bytes"):
            raise ValueError(f"Signed artifact '{name}' does not match the bundle manifest.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("sign", "verify"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--artifact", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--version", default="")
    args = parser.parse_args()
    artifacts: dict[str, Path] = {}
    try:
        for item in args.artifact:
            name, sep, path = item.partition("=")
            if not sep or name not in {"images", "models", "image_provenance"} or not path or name in artifacts:
                raise ValueError(f"Invalid artifact argument: {item}")
            artifacts[name] = Path(path)
        if args.action == "sign":
            sign(args.manifest, args.key, artifacts)
        else:
            if not args.version or "images" not in artifacts:
                raise ValueError("Verification requires --version and an images artifact.")
            verify(args.manifest, args.key, artifacts, args.version)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Bundle manifest {args.action} succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
