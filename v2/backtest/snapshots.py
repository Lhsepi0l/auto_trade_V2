from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _ReplayFrame:
    symbol: str
    market: dict[str, Any]
    meta: dict[str, Any]


@dataclass(frozen=True)
class _Kline15m:
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class _FundingRateRow:
    funding_time_ms: int
    funding_rate: float
