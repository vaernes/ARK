#!/usr/bin/env python3
"""
Lightweight stdlib-only Mock Inference Server for GPU-less ARK integration testing.
Emulates vLLM (/v1/chat/completions, /v1/models), TEI Embed (/v1/embeddings),
and TEI Rerank (/rerank) on port 8000 (and port 80 if configured).
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def deterministic_embedding(text: str, dim: int = 1024) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw = [((digest[i % len(digest)] / 255.0) * 2.0 - 1.0) for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [round(x / norm, 6) for x in raw]


class MockHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/health", "/readyz"):
            self._send_json(200, {"status": "ok", "mock": True})
        elif self.path == "/v1/models":
            self._send_json(200, {"object": "list", "data": [{"id": "ark-llm", "object": "model"}]})
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

        if self.path == "/v1/chat/completions":
            messages = body.get("messages", [])
            sys_msg = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
            user_msg = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
            has_rag = "Retrieved Context:" in sys_msg
            reply = (
                f"[MOCK-LLM] Received: '{user_msg}'. "
                f"RAG_CONTEXT_INJECTED={has_rag}."
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
