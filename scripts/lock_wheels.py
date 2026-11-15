#!/usr/bin/env python3
"""Generate a hash-locked requirements file from an installed Python environment."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: lock_wheels.py OUTPUT", file=sys.stderr)
        return 2
    result = subprocess.run([sys.executable, "-m", "pip", "freeze"], check=True, capture_output=True, text=True)
    pinned: dict[tuple[str, str], str] = {}
    for line in result.stdout.splitlines():
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)", line)
        if not match:
            raise ValueError(f"Cannot lock non-pinned dependency: {line}")
        pinned[(canonicalize_name(match.group(1)), match.group(2))] = match.group(1)
    with tempfile.TemporaryDirectory(prefix="ark-wheel-lock-") as tmp:
        root = Path(tmp)
        requirements = root / "requirements.txt"
        requirements.write_text(result.stdout, encoding="utf-8")
        wheels = root / "wheels"
        subprocess.run(
            [sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
             "--requirement", str(requirements), "--dest", str(wheels)],
            check=True,
        )
        hashes: dict[tuple[str, str], str] = {}
        for wheel in wheels.glob("*.whl"):
            name, version, _, _ = parse_wheel_filename(wheel.name)
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            hashes[(canonicalize_name(name), str(version))] = digest
        missing = pinned.keys() - hashes.keys()
        if missing:
            raise ValueError(f"Missing wheel hashes for: {sorted(missing)}")
        output = Path(sys.argv[1])
        output.write_text(
            "# Python 3.12 Linux x86_64; regenerate from a reviewed environment with scripts/lock_wheels.py.\n"
            + "".join(f"{pinned[key]}=={key[1]} --hash=sha256:{hashes[key]}\n" for key in sorted(pinned)),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
