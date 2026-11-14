"""Authentication, authorization, schedule, and message helpers for ark-admin."""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any


def normalize_groups(raw: str) -> set[str]:
    """Parse comma-separated Keycloak/oauth2-proxy groups, stripping leading slashes."""
    return {g.strip().lstrip("/") for g in (raw or "").split(",") if g.strip()}


def _is_subpath(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def authorize_proxy_request(
    path: str,
    headers: Mapping[str, str],
    expected_secret: str,
) -> tuple[bool, int, str]:
    """
    Enforce proxy authentication and group authorization on /admin-console* and /portal*.

    Returns (allowed, status_code, actor_or_reason).
    - When allowed is True, the third element is the authenticated actor identity.
    - When allowed is False, the second element is the HTTP status code and the third is the error detail.
    """
    is_admin_route = _is_subpath(path, "/admin-console")
    is_portal_route = _is_subpath(path, "/portal")

    if not is_admin_route and not is_portal_route:
        return True, 200, ""

    if not expected_secret:
        return False, 403, "Proxy authentication secret is not configured; direct access is disabled."

    # Case-insensitive header lookup for Mapping implementations (dict or Starlette Headers)
    lowered = {k.lower(): v for k, v in headers.items()}
    provided_secret = lowered.get("x-ark-proxy-secret", "")
    if not provided_secret or not hmac.compare_digest(provided_secret, expected_secret):
        return False, 403, "Forbidden: requests must originate from the authenticated proxy."

    actor = (lowered.get("x-forwarded-email") or lowered.get("x-forwarded-user") or "").strip()
    if not actor:
        return False, 401, "Missing authenticated user identity."

    if is_admin_route:
        groups = normalize_groups(lowered.get("x-forwarded-groups", ""))
        if "ark-admins" not in groups:
            return False, 403, "Forbidden: ark-admins group membership is required."

    return True, 200, actor


_SCHEDULE_PRESETS: dict[str, int] = {
    "@5m": 300,
    "@15m": 900,
    "@30m": 1800,
    "@hourly": 3600,
    "hourly": 3600,
    "@6h": 21600,
    "@12h": 43200,
    "@daily": 86400,
    "daily": 86400,
    "@weekly": 604800,
    "weekly": 604800,
}


def _parse_cron_field(field: str, min_val: int, max_val: int, *, wrap_sunday: bool = False) -> set[int] | None:
    allowed: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            return None
        if "/" in part:
            base, step_str = part.split("/", 1)
            if not step_str.isdigit() or int(step_str) <= 0:
                return None
            step = int(step_str)
        else:
            base = part
            step = 1

        if base == "*":
            start, end = min_val, max_val
        elif "-" in base:
            lo_str, hi_str = base.split("-", 1)
            if not lo_str.isdigit() or not hi_str.isdigit():
                return None
            start, end = int(lo_str), int(hi_str)
            if start > end:
                return None
        elif base.isdigit():
            val = int(base)
            start, end = val, (max_val if "/" in part else val)
        else:
            return None

        for v in range(start, end + 1, step):
            norm_v = 0 if (wrap_sunday and v == 7) else v
            if norm_v < min_val or norm_v > max_val:
                return None
            allowed.add(norm_v)
    return allowed or None


def parse_cron_expression(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]] | None:
    """Parse a standard 5-field cron expression (min hour dom month dow)."""
    fields = (expr or "").strip().split()
    if len(fields) != 5:
        return None
    mins = _parse_cron_field(fields[0], 0, 59)
    hours = _parse_cron_field(fields[1], 0, 23)
    doms = _parse_cron_field(fields[2], 1, 31)
    months = _parse_cron_field(fields[3], 1, 12)
    dows = _parse_cron_field(fields[4], 0, 6, wrap_sunday=True)
    if None in (mins, hours, doms, months, dows):
        return None
    assert mins and hours and doms and months and dows
    return mins, hours, doms, months, dows


def schedule_interval_seconds(expr: str) -> int | None:
    """Resolve interval shorthand presets (`@5m`, `@hourly`, `every:15m`) or simple repeating cron intervals."""
    cleaned = (expr or "").strip().lower()
    if not cleaned:
        return None
    if cleaned in _SCHEDULE_PRESETS:
        return _SCHEDULE_PRESETS[cleaned]
    m = re.fullmatch(r"(?:every:|@)(\d+)([mhd])", cleaned)
    if m:
        val = int(m.group(1))
        unit = m.group(2)
        mult = 86400 if unit == "d" else (3600 if unit == "h" else 60)
        return max(60, val * mult)
    fields = cleaned.split()
    if len(fields) == 5:
        m_step = re.fullmatch(r"\*/(\d+)", fields[0])
        if m_step and fields[1:] == ["*", "*", "*", "*"]:
            return max(60, int(m_step.group(1)) * 60)
        h_step = re.fullmatch(r"\*/(\d+)", fields[1])
        if fields[0].isdigit() and h_step and fields[2:] == ["*", "*", "*"]:
            return max(3600, int(h_step.group(1)) * 3600)
    return None


def is_schedule_due(expr: str, last_sync_at: datetime | None, now: datetime | None = None) -> bool:
    """Return True if an interval shorthand or 5-field cron schedule is due to run at `now`."""
    cur_now = now or datetime.now(timezone.utc)
    if cur_now.tzinfo is None:
        cur_now = cur_now.replace(tzinfo=timezone.utc)

    interval = schedule_interval_seconds(expr)
    if interval is not None and len((expr or "").strip().split()) != 5:
        return last_sync_at is None or (cur_now - last_sync_at).total_seconds() >= interval

    cron_parts = parse_cron_expression(expr)
    if not cron_parts:
        return False
    mins, hours, doms, months, dows = cron_parts
    fields = (expr or "").strip().split()
    dom_wild = fields[2] == "*"
    dow_wild = fields[4] == "*"

    start_anchor = (
        last_sync_at.astimezone(timezone.utc)
        if last_sync_at is not None
        else (cur_now - timedelta(minutes=1))
    )
    earliest = max(start_anchor, cur_now - timedelta(days=8))
    cur_min = cur_now.replace(second=0, microsecond=0)
    while cur_min >= earliest:
        # Python weekday(): Monday=0 .. Sunday=6 -> cron dow: Sunday=0, Monday=1 .. Saturday=6
        cron_dow = (cur_min.weekday() + 1) % 7
        if cur_min.month in months and cur_min.hour in hours and cur_min.minute in mins:
            dom_match = cur_min.day in doms
            dow_match = cron_dow in dows
            day_ok = (dom_match and dow_match) if (dom_wild or dow_wild) else (dom_match or dow_match)
            if day_ok and (last_sync_at is None or cur_min > start_anchor):
                return True
        cur_min -= timedelta(minutes=1)
    return False


def extract_message_text(content: Any) -> str:
    """Extract plain text from string or OpenAI multimodal array `content`."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {"text", "input_text"}:
                txt = item.get("text")
                if isinstance(txt, str) and txt:
                    parts.append(txt)
        return "\n".join(parts)
    return ""


def merge_system_and_context_messages(
    messages: list[dict[str, Any]],
    project_system_prompt: str,
    context_blocks: list[str],
) -> list[dict[str, Any]]:
    """
    Combine ARK project system prompt, client-supplied system/developer prompts,
    and retrieved RAG chunks ahead of all non-system conversation turns.
    """
    client_system_parts: list[str] = []
    other_messages: list[dict[str, Any]] = []
    for msg in messages:
        role = str(msg.get("role") or "").lower()
        if role in {"system", "developer"}:
            txt = extract_message_text(msg.get("content")).strip()
            if txt:
                client_system_parts.append(txt)
        else:
            other_messages.append(msg)

    sections: list[str] = []
    if (project_system_prompt or "").strip():
        sections.append(project_system_prompt.strip())
    if client_system_parts:
        sections.append("Client Instructions:\n" + "\n\n".join(client_system_parts))
    if context_blocks:
        sections.append("Retrieved Context:\n" + "\n---\n".join(context_blocks))

    combined_sys = "\n\n".join(sections)
    if combined_sys:
        return [{"role": "system", "content": combined_sys}] + other_messages
    return other_messages
