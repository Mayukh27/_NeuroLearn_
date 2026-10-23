"""
============================================================
Admin authentication gate (V2, Phase 4)

All V2 course/video catalog content is managed exclusively through
admin-only endpoints (routers/courses.py, routers/videos.py) — there is
no hardcoded seed catalog and no student-facing way to add arbitrary
content or URLs.

This is intentionally a lightweight, additive gate — a single shared key
compared against the `X-Admin-Key` header — rather than a new role added
to the existing student-facing auth/security.py system. For a single-LAN,
~160-client research deployment with one operator, a shared admin key is
the smallest change that satisfies "admin-only"; it does not touch or
weaken the existing participant JWT auth in any way.

Set ADMIN_API_KEY in backend/.env before deploying. If it is unset, admin
endpoints are refused entirely (fail closed) rather than left open.
============================================================
"""

import os
import hmac

from fastapi import Header, HTTPException
from loguru import logger


def _get_admin_api_key() -> str | None:
    # Read on every call (not cached at import) so it behaves the same way
    # VIDEO_STORAGE_DIR does in config/video_config.py — tests and
    # deployments can set it without reimporting the module.
    #
    # .strip(): a hand-edited .env file (Notepad, VS Code autosave, etc.)
    # very commonly ends up with a trailing space or a stray \r — that
    # then silently fails hmac.compare_digest against a key typed into
    # the browser without it, showing as "invalid key" with no clue why.
    # Stripping both sides consistently doesn't weaken the comparison
    # (an attacker still needs the exact key); it just removes a footgun.
    raw = os.environ.get("ADMIN_API_KEY")
    return raw.strip() if raw else raw


def is_valid_admin_key(candidate: str) -> bool:
    """Boolean check, no exception — for endpoints where the admin key is
    one of several acceptable credentials (see routers/courses.py's
    student_or_admin) rather than the sole gate. require_admin (below)
    is still what actually protects mutating admin endpoints; this
    shares its exact comparison logic so there's one definition of
    "valid", not two that could drift apart.
    """
    expected = _get_admin_api_key()
    if not expected:
        return False
    received = (candidate or "").strip()
    return bool(received) and hmac.compare_digest(received, expected)


async def require_admin(x_admin_key: str = Header(default="")) -> None:
    """FastAPI dependency — raises 401/403 unless the request carries a
    valid X-Admin-Key header. Use via `Depends(require_admin)` on any
    endpoint that creates/deletes catalog content.
    """
    expected = _get_admin_api_key()
    if not expected:
        raise HTTPException(
            status_code=403,
            detail="Admin endpoints are disabled: ADMIN_API_KEY is not configured on the server.",
        )
    received = (x_admin_key or "").strip()
    if not received or not hmac.compare_digest(received, expected):
        logger.warning(
            "Admin key rejected — server key: {} chars (starts '{}…', ends '…{}'); "
            "received: {} chars{}. If length/prefix/suffix all match but this still "
            "fails, check for a trailing space or CRLF line ending in backend/.env.",
            len(expected), expected[:2], expected[-2:],
            len(received),
            f" (starts '{received[:2]}…', ends '…{received[-2:]}')" if received else " (empty)",
        )
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Key header.")