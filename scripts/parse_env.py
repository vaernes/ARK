#!/usr/bin/env python3
"""Read the supported Compose .env syntax without executing shell code."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def parse_value(raw: str) -> str:
    if not raw:
        return ""
    if raw[0] not in "'\"":
        value = re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
        if "$" in value:
            raise ValueError("quote literal dollar signs with single quotes")
        return value

    quote = raw[0]
    chars: list[str] = []
    index = 1
    while index < len(raw):
        char = raw[index]
        if char == quote:
            tail = raw[index + 1 :].strip()
            if tail and not tail.startswith("#"):
                raise ValueError("unexpected text after quoted value")
            value = "".join(chars)
            if quote == '"' and "$" in value:
                raise ValueError("quote literal dollar signs with single quotes")
            return value
        if char == "\\" and index + 1 < len(raw):
            following = raw[index + 1]
            escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}
            if quote == '"' and following in escapes:
                chars.append(escapes[following])
                index += 2
                continue
            if quote == "'" and following == "'":
                chars.append("'")
                index += 2
                continue
        chars.append(char)
        index += 1
    raise ValueError("unterminated quoted value")


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = ASSIGNMENT.fullmatch(line)
        if not match:
            raise ValueError(f"{path}:{number}: expected KEY=value")
        try:
            value = parse_value(match.group(2).lstrip())
            if "\x00" in value:
                raise ValueError("NUL is not allowed in an environment value")
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
        values[match.group(1)] = value
    return values


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: parse_env.py PATH", file=sys.stderr)
        return 2
    try:
        values = parse_env(Path(sys.argv[1]))
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for key, value in values.items():
        sys.stdout.buffer.write(key.encode() + b"\0" + value.encode() + b"\0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
