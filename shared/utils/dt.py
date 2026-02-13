from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()
