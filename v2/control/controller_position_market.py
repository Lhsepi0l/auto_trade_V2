from __future__ import annotations

import time
from typing import Any

from v2.control.runtime_utils import to_float


def extract_latest_market_bar(snapshot: dict[str, Any], *, symbol: str) -> dict[str, float] | None:
    symbols_payload = snapshot.get("symbols")
    market = None
    if isinstance(symbols_payload, dict):
        market = symbols_payload.get(symbol)
    if not isinstance(market, dict):
        market = snapshot.get("market")
    if not isinstance(market, dict):
        return None
    candles = market.get("15m")
    if not isinstance(candles, list) or not candles:
        return None
    row = candles[-1]
    if isinstance(row, dict):
        open_time = to_float(row.get("open_time") or row.get("openTime") or row.get("t"), default=0.0)
        close_time = to_float(row.get("close_time") or row.get("closeTime") or row.get("T"), default=0.0)
        open_v = to_float(row.get("open"), default=0.0)
        high_v = to_float(row.get("high"), default=0.0)
        low_v = to_float(row.get("low"), default=0.0)
        close_v = to_float(row.get("close"), default=0.0)
    elif isinstance(row, (list, tuple)) and len(row) >= 7:
        open_time = to_float(row[0], default=0.0)
        open_v = to_float(row[1], default=0.0)
        high_v = to_float(row[2], default=0.0)
        low_v = to_float(row[3], default=0.0)
        close_v = to_float(row[4], default=0.0)
        close_time = to_float(row[6], default=0.0)
    else:
        return None
    if close_v <= 0.0:
        return None
    return {
        "open_time_ms": float(open_time),
        "close_time_ms": float(close_time),
        "open": float(open_v),
        "high": float(high_v),
        "low": float(low_v),
        "close": float(close_v),
    }


def bars_held_for_management(plan: dict[str, Any], bar: dict[str, float] | None) -> int:
    entry_time_ms = int(to_float(plan.get("entry_time_ms"), default=0.0))
    if entry_time_ms <= 0:
        return 0
    current_time_ms = int(to_float((bar or {}).get("close_time_ms"), default=0.0)) if isinstance(bar, dict) else 0
    if current_time_ms <= 0:
        current_time_ms = int(time.time() * 1000)
    return max((current_time_ms - entry_time_ms) // (15 * 60 * 1000), 0)
