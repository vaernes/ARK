"""Provider-agnostic OpenAI-compatible LLM backend configuration helpers."""

from __future__ import annotations

import os
from typing import Mapping

VALID_BACKEND_TYPES = {"ark_llm", "third_party"}


def normalize_backend_type(value: str | None, *, default: str = "ark_llm") -> str:
    """Normalize LLM backend type to 'ark_llm' or 'third_party'."""
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in VALID_BACKEND_TYPES else default


def resolve_llm_backend_config(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Resolve OpenAI-compatible LLM backend settings with legacy VLLM_* fallback."""
    source = os.environ if env is None else env

    raw_base = (source.get("LLM_API_BASE_URL") or "").strip().rstrip("/")
    if not raw_base:
        vllm_url = (source.get("VLLM_URL") or "http://vllm:8000").strip().rstrip("/")
        raw_base = vllm_url if vllm_url.endswith("/v1") else f"{vllm_url}/v1"

    api_key = (source.get("LLM_API_KEY") or source.get("VLLM_API_KEY") or "").strip()
    health_url = (source.get("LLM_HEALTH_URL") or "").strip() or f"{raw_base}/models"
    backend_type = normalize_backend_type(source.get("LLM_BACKEND_TYPE"))
    backend_admin_url = (source.get("LLM_BACKEND_ADMIN_URL") or "").strip().rstrip("/")

    return {
        "backend_type": backend_type,
        "api_base_url": raw_base,
        "api_key": api_key,
        "health_url": health_url,
        "backend_admin_url": backend_admin_url if backend_type == "ark_llm" else "",
    }


def build_llm_headers(api_key: str, *, json_content: bool = False) -> dict[str, str]:
    """Build upstream HTTP headers for OpenAI-compatible LLM requests."""
    headers: dict[str, str] = {}
    if json_content:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers
