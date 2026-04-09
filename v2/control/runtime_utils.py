from __future__ import annotations

import logging
from concurrent.futures import TimeoutError as _FutureTimeoutError
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from v2.common.async_bridge import run_async_blocking as _run_async_blocking_impl

logger = logging.getLogger("v2.control.api")
FutureTimeoutError = _FutureTimeoutError


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def clamp(value: float, low: float, high: float) -> float:
    if value < low:
        return low
    if value > high:
        return high
    return value


def normalize_pct(value: Any, default: float = 0.0) -> float:
    parsed = abs(to_float(value, default=default))
    if parsed > 1.0:
        parsed = parsed / 100.0
    return max(parsed, 0.0)


def parse_iso_datetime(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def age_seconds(raw: Any) -> float | None:
    parsed = parse_iso_datetime(raw)
    if parsed is None:
        return None
    return max((datetime.now(timezone.utc) - parsed).total_seconds(), 0.0)


def run_async_blocking(
    thunk: Callable[[], Coroutine[Any, Any, Any]], *, timeout_sec: float | None = None
) -> Any:
    return _run_async_blocking_impl(thunk, timeout_sec=timeout_sec)
