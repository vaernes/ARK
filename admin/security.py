"""Authentication and authorization helpers for ark-admin proxy boundaries."""

from __future__ import annotations

import hmac
from collections.abc import Mapping


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
