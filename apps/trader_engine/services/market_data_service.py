from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from apps.trader_engine.exchange.binance_usdm import BinanceUSDMClient
from shared.utils.retry import retry

logger = logging.getLogger(__name__)

SUPPORTED_KLINE_INTERVALS = {
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
    "1M",
}

_INTERVAL_ALIASES = {
    # "10m" is intentionally unsupported by Binance klines on this endpoint.
    "10m": "15m",
    "60m": "1h",
    "120m": "2h",
    "240m": "4h",
}


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int


def _as_float(x: Any) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


def _as_int(x: Any) -> int:
    try:
        return int(x)
    except Exception:
        return 0


def _parse_klines(rows: List[List[Any]]) -> List[Candle]:
    out: List[Candle] = []
    for r in rows:
        # Binance kline row layout:
        # [0] open_time, [1] open, [2] high, [3] low, [4] close, [5] volume,
        # [6] close_time, ...
        if len(r) < 7:
            continue
        out.append(
            Candle(
                open_time_ms=_as_int(r[0]),
                open=_as_float(r[1]),
                high=_as_float(r[2]),
                low=_as_float(r[3]),
                close=_as_float(r[4]),
                volume=_as_float(r[5]),
                close_time_ms=_as_int(r[6]),
            )
        )
    return out


class MarketDataService:
    """Minimal in-memory kline cache for scheduler/decision service.

    NOTE: Binance client uses synchronous requests; this service stays sync too.
    The scheduler should call it via asyncio.to_thread().
    """

    def __init__(
        self,
        *,
        client: BinanceUSDMClient,
        cache_ttl_sec: float = 20.0,
        retry_attempts: int = 3,
        retry_backoff_sec: float = 0.25,
    ) -> None:
        self._client = client
        self._cache_ttl_sec = float(cache_ttl_sec)
        self._retry_attempts = int(retry_attempts)
        self._retry_backoff_sec = float(retry_backoff_sec)
        self._interval_fallback_logged: set[str] = set()
        # key: (symbol, interval, limit) -> (fetched_at_ms, candles)
        self._cache: Dict[Tuple[str, str, int], Tuple[int, List[Candle]]] = {}

    @staticmethod
    def _normalize_interval(interval: str) -> str:
        itv = str(interval).strip()
        if not itv:
            raise ValueError("interval is empty")

        if itv in SUPPORTED_KLINE_INTERVALS:
            return itv

        fallback = _INTERVAL_ALIASES.get(itv)
        if fallback is None:
            raise ValueError(f"unsupported interval: {itv}")
        return fallback

    def get_klines(self, *, symbol: str, interval: str, limit: int = 200) -> List[Candle]:
        sym = symbol.strip().upper()
        raw_itv = str(interval).strip()
        itv = self._normalize_interval(raw_itv)
        if itv != raw_itv:
            warn_key = f"{sym}:{raw_itv}->{itv}"
            if warn_key not in self._interval_fallback_logged:
                logger.warning(
                    "market_data_interval_fallback",
                    extra={"symbol": sym, "requested_interval": raw_itv, "resolved_interval": itv},
                )
                self._interval_fallback_logged.add(warn_key)
        lim = int(limit)
        key = (sym, itv, lim)
        now_ms = int(time.time() * 1000)
        cached = self._cache.get(key)
        if cached:
            fetched_at_ms, candles = cached
            if (now_ms - fetched_at_ms) <= int(self._cache_ttl_sec * 1000):
                return list(candles)

        def _fetch():
            return self._client.get_klines(symbol=sym, interval=itv, limit=lim)

        rows = retry(_fetch, attempts=self._retry_attempts, base_delay_sec=self._retry_backoff_sec)
        candles = _parse_klines(rows)
        if not candles:
            logger.warning("klines_empty", extra={"symbol": sym, "interval": itv, "limit": lim})
        self._cache[key] = (now_ms, candles)
        return list(candles)

    def get_last_close(self, *, symbol: str, interval: str, limit: int = 2) -> Optional[float]:
        candles = self.get_klines(symbol=symbol, interval=interval, limit=limit)
        if not candles:
            return None
        return float(candles[-1].close)
