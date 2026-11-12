#!/usr/bin/env python3
"""
Lightweight stdlib-only Mock Inference Server for GPU-less ARK integration testing.
Emulates vLLM (/v1/chat/completions, /v1/models), TEI Embed (/v1/embeddings),
and TEI Rerank (/rerank) on port 8000 (and port 80 if configured).
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
import sys
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def deterministic_embedding(text: str, dim: int = 1024) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw = [((digest[i % len(digest)] / 255.0) * 2.0 - 1.0) for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [round(x / norm, 6) for x in raw]


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def generate_mock_png_b64(prompt: str, size: str = "1024x1024", index: int = 0) -> str:
    """Generate a valid, deterministic RGB PNG image in base64 using stdlib only."""
    digest = hashlib.sha256(f"{prompt}|{size}|{index}".encode("utf-8")).digest()
    width, height = 256, 256
    if "x" in (size or ""):
        try:
            w_raw, h_raw = (int(x) for x in size.lower().split("x", 1))
            if w_raw > h_raw:
                width, height = 320, 192
            elif h_raw > w_raw:
                width, height = 192, 320
        except ValueError:
            pass

    cx, cy = width * 0.5, height * 0.5
    ring1 = min(width, height) * (0.16 + (digest[0] % 8) * 0.01)
    ring2 = min(width, height) * (0.31 + (digest[1] % 8) * 0.01)
    accent_amber = (digest[2] % 2 == 0)
    rows: list[bytes] = []
    for y in range(height):
        row = bytearray([0])  # filter type 0 (None)
        dy = y - cy
        for x in range(width):
            dx = x - cx
            dist = math.hypot(dx, dy)
            r, g, b = 10, 16, 28  # Deep Void base (#0A101C)

            # Subtle isometric/blueprint grid
            if x % 16 == 0 or y % 16 == 0:
                r, g, b = 18, 34, 56
            if x % 64 == 0 or y % 64 == 0:
                r, g, b = 26, 56, 88

            # Diagonal data-bus rays
            diag1 = abs((dx * 0.6 - dy) if accent_amber else (dx + dy * 0.6))
            diag2 = abs((dx * 0.6 + dy) if accent_amber else (dx - dy * 0.6))
            if (diag1 < 1.8 or diag2 < 1.8) and ring1 < dist < ring2:
                r, g, b = 0, 185, 220

            # Outer enclave shield ring (Tech Cyan #00E5FF)
            if abs(dist - ring2) < 2.2:
                r, g, b = 0, 229, 255
            elif dist < ring2 and (abs(dx) < 24 and abs(dy) < 24):
                # Central compute core block
                if abs(abs(dx) - 22) < 2 or abs(abs(dy) - 22) < 2:
                    r, g, b = 245, 90, 0  # Signal Amber (#F55A00)
                else:
                    r, g, b = 22, 42, 68

            # Inner vector ring (Signal Amber #F55A00)
            if abs(dist - ring1) < 2.2:
                r, g, b = 245, 90, 0

            # Satellite node hubs
            for nx, ny in ((-ring2 * 0.68, -ring2 * 0.42), (ring2 * 0.68, -ring2 * 0.42), (-ring2 * 0.68, ring2 * 0.42), (ring2 * 0.68, ring2 * 0.42)):
                nd = math.hypot(dx - nx, dy - ny)
                if nd < 9.0:
                    if nd > 6.5:
                        r, g, b = 0, 229, 255
                    else:
                        r, g, b = 30, 58, 95

            row.extend((r, g, b))
        rows.append(bytes(row))

    raw_data = b"".join(rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw_data, level=6))
        + _png_chunk(b"IEND", b"")
    )
    return base64.b64encode(png_bytes).decode("ascii")


class MockHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/health", "/readyz", "/tika"):
            self._send_json(200, {"status": "ok", "mock": True})
        elif self.path == "/v1/models":
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "ark-llm", "object": "model"},
                        {"id": "ark-image", "object": "model"},
                    ],
                },
            )
        else:
            self._send_json(404, {"error": f"Unknown path: {self.path}"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")

        if self.path == "/v1/embeddings":
            inputs = body.get("input", [])
            if isinstance(inputs, str):
                inputs = [inputs]
            data = [
                {"object": "embedding", "index": i, "embedding": deterministic_embedding(str(txt))}
                for i, txt in enumerate(inputs)
            ]
            self._send_json(200, {"object": "list", "data": data, "model": body.get("model", "BAAI/bge-m3")})
            return

        if self.path == "/rerank":
            texts = body.get("texts", [])
            results = [{"index": i, "score": round(1.0 / (i + 1), 4)} for i in range(len(texts))]
            self._send_json(200, results)
            return

        if self.path == "/v1/images/generations":
            prompt = str(body.get("prompt") or "").strip()
            if not prompt:
                self._send_json(400, {"error": {"message": "prompt is required"}})
                return
            model_name = str(body.get("model") or "ark-image")
            size = str(body.get("size") or "1024x1024")
            n = max(1, min(int(body.get("n") or 1), 4))
            created = int(time.time())
            data = [
                {
                    "b64_json": generate_mock_png_b64(prompt, size, i),
                    "revised_prompt": f"[MOCK-IMAGE:{size}] {prompt}",
                }
                for i in range(n)
            ]
            self._send_json(200, {"created": created, "model": model_name, "data": data})
            return

        if self.path == "/v1/chat/completions":
            def _msg_text(val: Any) -> str:
                if isinstance(val, str):
                    return val
                if isinstance(val, list):
                    parts = []
                    for item in val:
                        if isinstance(item, str):
                            parts.append(item)
                        elif isinstance(item, dict) and item.get("type") == "text":
                            parts.append(str(item.get("text", "")))
                    return " ".join(p for p in parts if p).strip()
                return str(val or "")

            messages = body.get("messages", [])
            sys_msg = "\n".join(
                _msg_text(m.get("content", ""))
                for m in messages
                if m.get("role") == "system"
            )
            user_msg = next(
                (_msg_text(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"),
                "",
            )
            has_rag = "Retrieved Context:" in sys_msg
            has_client = "Client Instructions:" in sys_msg
            reply = (
                f"[MOCK-LLM] Received: '{user_msg}'. "
                f"RAG_CONTEXT_INJECTED={has_rag}. "
                f"HAS_CLIENT_INSTRUCTIONS={has_client}."
            )
            model_name = body.get("model", "ark-llm")
            cmpl_id = f"chatcmpl-mock-{int(time.time())}"
            created = int(time.time())

            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                chunk = {
                    "id": cmpl_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": reply},
                            "finish_reason": "stop",
                        }
                    ],
                }
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return

            self._send_json(
                200,
                {
                    "id": cmpl_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": reply},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 8,
                        "total_tokens": 20,
                    },
                },
            )
            return

        self._send_json(404, {"error": f"Unknown path: {self.path}"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("0.0.0.0", port), MockHandler)
    print(f"Mock inference server listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
