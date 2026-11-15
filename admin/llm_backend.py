"""Provider-agnostic OpenAI-compatible LLM backend configuration helpers."""

from __future__ import annotations

import os
from typing import Mapping

VALID_BACKEND_TYPES = {"ark_llm", "third_party"}
VALID_MODEL_CONTEXTS = ("text", "image", "code", "other")
MODEL_CONTEXT_LABELS: dict[str, str] = {
    "text": "Text / Chat",
    "image": "Image Generation",
    "code": "Code",
    "other": "Other",
}

THIRD_PARTY_LLM_PRESETS: dict[str, dict[str, str]] = {
    "huggingface": {
        "id": "huggingface",
        "label": "Hugging Face",
        "api_base_url": "https://router.huggingface.co/v1",
        "default_model": "Qwen/Qwen2.5-72B-Instruct",
        "key_placeholder": "hf_...",
    },
    "anthropic": {
        "id": "anthropic",
        "label": "Anthropic (Claude)",
        "api_base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-sonnet-4-20250514",
        "key_placeholder": "sk-ant-...",
    },
    "openai": {
        "id": "openai",
        "label": "OpenAI",
        "api_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "default_image_model": "dall-e-3",
        "key_placeholder": "sk-...",
    },
    "google": {
        "id": "google",
        "label": "Google (Gemini)",
        "api_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.5-flash",
        "default_image_model": "imagen-3.0-generate-002",
        "key_placeholder": "AIza...",
    },
    "custom": {
        "id": "custom",
        "label": "Custom OpenAI-Compatible",
        "api_base_url": "",
        "default_model": "",
        "key_placeholder": "Bearer API key (optional)",
    },
}


def normalize_backend_type(value: str | None, *, default: str = "ark_llm") -> str:
    """Normalize LLM backend type to 'ark_llm' or 'third_party'."""
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in VALID_BACKEND_TYPES else default


def normalize_third_party_provider(provider: str | None, api_base_url: str | None = None) -> str:
    """Resolve a 3rd-party LLM provider identifier from explicit value or URL."""
    cleaned = (provider or "").strip().lower()
    alias_map = {
        "hf": "huggingface",
        "hugging_face": "huggingface",
        "claude": "anthropic",
        "gemini": "google",
    }
    cleaned = alias_map.get(cleaned, cleaned)
    if cleaned in THIRD_PARTY_LLM_PRESETS and cleaned != "custom":
        return cleaned

    url = (api_base_url or "").strip().lower()
    if "huggingface.co" in url:
        return "huggingface"
    if "anthropic.com" in url:
        return "anthropic"
    if "api.openai.com" in url:
        return "openai"
    if "generativelanguage.googleapis.com" in url:
        return "google"
    return "custom"


def resolve_llm_backend_config(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Resolve OpenAI-compatible LLM backend settings with legacy VLLM_* fallback."""
    source = os.environ if env is None else env

    backend_type = normalize_backend_type(source.get("LLM_BACKEND_TYPE"))
    raw_provider = (source.get("LLM_PROVIDER") or "").strip()
    raw_base = (source.get("LLM_API_BASE_URL") or "").strip().rstrip("/")
    if not raw_base and backend_type == "third_party" and raw_provider:
        preset_key = normalize_third_party_provider(raw_provider)
        raw_base = THIRD_PARTY_LLM_PRESETS.get(preset_key, {}).get("api_base_url", "")
    if not raw_base:
        vllm_url = (source.get("VLLM_URL") or "http://vllm:8000").strip().rstrip("/")
        raw_base = vllm_url if vllm_url.endswith("/v1") else f"{vllm_url}/v1"

    api_key = (source.get("LLM_API_KEY") or source.get("VLLM_API_KEY") or "").strip()
    health_url = (source.get("LLM_HEALTH_URL") or "").strip() or f"{raw_base}/models"
    backend_admin_url = (source.get("LLM_BACKEND_ADMIN_URL") or "").strip().rstrip("/")
    provider = normalize_third_party_provider(raw_provider, raw_base) if backend_type == "third_party" else "ark_llm"

    return {
        "backend_type": backend_type,
        "provider": provider,
        "api_base_url": raw_base,
        "api_key": api_key,
        "health_url": health_url,
        "backend_admin_url": backend_admin_url if backend_type == "ark_llm" else "",
    }


def resolve_image_backend_config(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Resolve OpenAI-compatible Image Generation (/v1/images/generations) settings."""
    source = os.environ if env is None else env
    llm_fallback = resolve_llm_backend_config(source)

    raw_base = (source.get("IMAGE_API_BASE_URL") or "").strip().rstrip("/")
    if not raw_base:
        raw_base = llm_fallback["api_base_url"]

    if "IMAGE_API_KEY" in source and (source.get("IMAGE_API_KEY") or "").strip():
        api_key = (source.get("IMAGE_API_KEY") or "").strip()
    else:
        api_key = llm_fallback["api_key"]

    default_model = (
        (source.get("IMAGE_SERVED_NAME") or "").strip()
        or (source.get("IMAGE_MODEL_ID") or "").strip()
        or "ark-image"
    )
    health_url = (source.get("IMAGE_HEALTH_URL") or "").strip() or f"{raw_base}/models"

    return {
        "api_base_url": raw_base,
        "api_key": api_key,
        "default_model": default_model,
        "health_url": health_url,
    }


CREATE_STYLE_PRESETS: dict[str, dict[str, str]] = {
    "photorealistic": {
        "id": "photorealistic",
        "label": "Photorealistic",
        "description": "Studio-grade photography with natural lighting and crisp detail",
        "prompt_suffix": "Style: editorial studio photography, natural lighting, sharp focus, high dynamic range, photorealistic detail.",
    },
    "illustration": {
        "id": "illustration",
        "label": "Digital Illustration",
        "description": "Clean modern vector illustration for articles and documentation",
        "prompt_suffix": "Style: modern digital vector illustration, clean geometric forms, balanced enterprise palette, crisp edges.",
    },
    "isometric_diagram": {
        "id": "isometric_diagram",
        "label": "Isometric Diagram",
        "description": "3D isometric technical architecture visual with slate, cyan, and amber accents",
        "prompt_suffix": "Style: isometric 3D technical architecture diagram, dark slate background (#0A0E17), glowing cyan (#00E5FF) and signal amber (#F55A00) highlights, clean precision lines.",
    },
    "icon": {
        "id": "icon",
        "label": "App / UI Icon",
        "description": "Centered minimalist emblem or product icon on a clean dark canvas",
        "prompt_suffix": "Style: minimalist app icon emblem, centered composition, bold silhouette, subtle metallic and cyan gradient on dark slate background.",
    },
    "slide_graphic": {
        "id": "slide_graphic",
        "label": "Slide Graphic",
        "description": "Executive presentation hero visual with uncluttered composition",
        "prompt_suffix": "Style: widescreen executive presentation visual, modern abstract enterprise tech aesthetic, high contrast, uncluttered composition.",
    },
    "blueprint": {
        "id": "blueprint",
        "label": "Technical Blueprint",
        "description": "CAD-style schematic wireframe and engineering blueprint",
        "prompt_suffix": "Style: technical engineering schematic blueprint, precision CAD wireframe lines, grid overlay, dark navy and cyan schematic aesthetic.",
    },
    "none": {
        "id": "none",
        "label": "Raw Prompt",
        "description": "Use your exact prompt without style modifiers",
        "prompt_suffix": "",
    },
}

CREATE_SUPPORTED_SIZES: list[dict[str, str]] = [
    {"size": "1024x1024", "aspect": "1:1", "label": "Square (1024x1024)"},
    {"size": "1344x768", "aspect": "16:9", "label": "Landscape / Slide (1344x768)"},
    {"size": "768x1344", "aspect": "9:16", "label": "Portrait (768x1344)"},
]


def apply_create_style_preset(prompt: str, style_preset: str | None) -> tuple[str, str]:
    """Return (normalized_preset_id, styled_prompt)."""
    cleaned_prompt = (prompt or "").strip()
    preset_key = (style_preset or "none").strip().lower()
    if preset_key not in CREATE_STYLE_PRESETS:
        preset_key = "none"
    suffix = CREATE_STYLE_PRESETS[preset_key]["prompt_suffix"]
    if not suffix:
        return preset_key, cleaned_prompt
    return preset_key, f"{cleaned_prompt}\n\n{suffix}"


def build_llm_headers(
    api_key: str,
    *,
    json_content: bool = False,
    provider: str = "",
    api_base_url: str = "",
) -> dict[str, str]:
    """Build upstream HTTP headers for OpenAI-compatible LLM requests."""
    headers: dict[str, str] = {}
    if json_content:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        effective_provider = (
            normalize_third_party_provider(provider, api_base_url)
            if (provider and provider != "ark_llm") or "anthropic.com" in (api_base_url or "").lower()
            else ""
        )
        if effective_provider == "anthropic":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
    return headers
