from __future__ import annotations

import re
import secrets
import string
import time

_ALLOWED = string.ascii_letters + string.digits + "-_"
_DISALLOWED_RE = re.compile(r"[^A-Za-z0-9\-_]")


def sanitize_client_order_id(raw: str, *, max_length: int = 31) -> str:
    cleaned = _DISALLOWED_RE.sub("-", str(raw or "")).strip("-")
    if not cleaned:
        cleaned = "cid"
    return cleaned[:max_length]


def generate_client_order_id(*, prefix: str = "v2", max_length: int = 31) -> str:
    ts = format(int(time.time() * 1000), "x")
    rnd = "".join(secrets.choice(_ALLOWED) for _ in range(8))
    base = sanitize_client_order_id(prefix, max_length=max_length)
    value = f"{base}-{ts}-{rnd}" if base else f"{ts}-{rnd}"
    return sanitize_client_order_id(value, max_length=max_length)
