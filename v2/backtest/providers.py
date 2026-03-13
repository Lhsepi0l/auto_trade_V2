from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

from v2.backtest.snapshots import _FundingRateRow, _Kline15m, _ReplayFrame
from v2.strategies.alpha_shared import _Bar


def _zscore_latest(values: list[float], lookback: int) -> float | None:
    window = [float(item) for item in values[-max(int(lookback), 1) :]]
    if len(window) < max(int(lookback), 2):
        return None
    mean_value = sum(window) / float(len(window))
    variance = sum((item - mean_value) ** 2 for item in window) / float(len(window))
    stdev = variance**0.5
    if stdev <= 1e-12:
        return 0.0
    return (float(window[-1]) - float(mean_value)) / stdev


def _sum_recent_funding(
    rows: list[_FundingRateRow],
    *,
    current_time_ms: int,
    window_ms: int,
) -> float | None:
    relevant = [
        float(row.funding_rate)
        for row in rows
        if int(row.funding_time_ms) <= int(current_time_ms)
        and int(row.funding_time_ms) > int(current_time_ms) - int(window_ms)
    ]
    if not relevant:
        return None
    return sum(relevant)


class _HistoricalSnapshotProvider:
    def __init__(
        self,
        *,
        symbol: str,
        candles_15m: list[_Kline15m],
        market_candles: dict[str, list[_Kline15m]] | None = None,
        premium_rows_15m: list[_Kline15m] | None = None,
        funding_rows: list[_FundingRateRow] | None = None,
        market_intervals: list[str] | None = None,
        candles_10m: list[_Kline15m] | None = None,
        candles_30m: list[_Kline15m] | None = None,
        candles_1h: list[_Kline15m] | None = None,
        candles_4h: list[_Kline15m] | None = None,
        history_limit: int = 260,
    ) -> None:
        self._symbol = symbol
        self._candles_15m = sorted(list(candles_15m), key=lambda row: int(row.open_time_ms))
        self._idx = -1
        limit = max(int(history_limit), 1)

        legacy_market_candles: dict[str, list[_Kline15m]] = {}
        if candles_10m is not None:
            legacy_market_candles["10m"] = list(candles_10m)
        if candles_30m is not None:
            legacy_market_candles["30m"] = list(candles_30m)
        if candles_1h is not None:
            legacy_market_candles["1h"] = list(candles_1h)
        if candles_4h is not None:
            legacy_market_candles["4h"] = list(candles_4h)

        merged_market: dict[str, list[_Kline15m]] = {}
        if market_candles is not None:
            for interval, rows in market_candles.items():
                key = str(interval).strip()
                if not key:
                    continue
                merged_market[key] = list(rows)
        for interval, rows in legacy_market_candles.items():
            merged_market.setdefault(interval, rows)

        configured_intervals = []
        if isinstance(market_intervals, list):
            for raw_interval in market_intervals:
                interval = str(raw_interval).strip()
                if interval:
                    configured_intervals.append(interval)
        if not configured_intervals:
            configured_intervals = ["10m", "15m", "30m", "1h", "4h"]
            for interval in merged_market.keys():
                interval_key = str(interval).strip()
                if interval_key and interval_key not in configured_intervals:
                    configured_intervals.append(interval_key)
        if "15m" not in configured_intervals:
            configured_intervals.insert(0, "15m")

        seen_intervals: set[str] = set()
        ordered_intervals: list[str] = []
        for interval in configured_intervals:
            if interval in seen_intervals:
                continue
            seen_intervals.add(interval)
            ordered_intervals.append(interval)
        self._intervals = ordered_intervals

        self._histories: dict[str, deque[dict[str, float]]] = {
            interval: deque(maxlen=limit) for interval in self._intervals
        }
        self._sources: dict[str, list[_Kline15m]] = {}
        self._source_index: dict[str, int] = {}
        self._premium_source = sorted(
            list(premium_rows_15m or []),
            key=lambda row: int(row.open_time_ms),
        )
        self._premium_index = -1
        self._premium_history: deque[_Kline15m] = deque(maxlen=max(limit, 320))
        self._funding_source = sorted(
            list(funding_rows or []),
            key=lambda row: int(row.funding_time_ms),
        )
        self._funding_index = -1
        self._funding_history: deque[_FundingRateRow] = deque(maxlen=32)
        for interval in self._intervals:
            if interval == "15m":
                continue
            rows = merged_market.get(interval, [])
            self._sources[interval] = sorted(list(rows), key=lambda row: int(row.open_time_ms))
            self._source_index[interval] = -1

    @staticmethod
    def _row_to_ohlc(row: _Kline15m) -> dict[str, float]:
        return {
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume),
        }

    def _advance_interval(self, *, interval: str, current_close_time_ms: int) -> None:
        source = self._sources.get(interval, [])
        history = self._histories[interval]
        idx = int(self._source_index.get(interval, -1))
        while idx + 1 < len(source):
            nxt = source[idx + 1]
            if int(nxt.close_time_ms) > int(current_close_time_ms):
                break
            idx += 1
            history.append(self._row_to_ohlc(nxt))
        self._source_index[interval] = idx

    def _advance_premium(self, *, current_close_time_ms: int) -> None:
        idx = int(self._premium_index)
        while idx + 1 < len(self._premium_source):
            nxt = self._premium_source[idx + 1]
            if int(nxt.close_time_ms) > int(current_close_time_ms):
                break
            idx += 1
            self._premium_history.append(nxt)
        self._premium_index = idx

    def _advance_funding(self, *, current_close_time_ms: int) -> None:
        idx = int(self._funding_index)
        while idx + 1 < len(self._funding_source):
            nxt = self._funding_source[idx + 1]
            if int(nxt.funding_time_ms) > int(current_close_time_ms):
                break
            idx += 1
            self._funding_history.append(nxt)
        self._funding_index = idx

    def __len__(self) -> int:
        return len(self._candles_15m)

    def candle_at(self, idx: int) -> _Kline15m:
        return self._candles_15m[idx]

    def __call__(self) -> dict[str, Any]:
        self._idx += 1
        if self._idx >= len(self._candles_15m):
            return {}
        row = self._candles_15m[self._idx]
        self._histories["15m"].append(self._row_to_ohlc(row))

        for interval in self._intervals:
            if interval == "15m":
                continue
            self._advance_interval(interval=interval, current_close_time_ms=row.close_time_ms)
        self._advance_premium(current_close_time_ms=row.close_time_ms)
        self._advance_funding(current_close_time_ms=row.close_time_ms)

        market_payload = {
            interval: list(self._histories[interval]) for interval in self._intervals
        }
        premium_rows = list(self._premium_history)
        funding_rows = list(self._funding_history)
        market_payload["premium"] = {
            "close_15m": float(premium_rows[-1].close) if premium_rows else None,
            "zscore_24h": (
                _zscore_latest([float(item.close) for item in premium_rows], 96)
                if premium_rows
                else None
            ),
            "zscore_3d": (
                _zscore_latest([float(item.close) for item in premium_rows], 288)
                if premium_rows
                else None
            ),
        }
        market_payload["funding"] = {
            "last": float(funding_rows[-1].funding_rate) if funding_rows else None,
            "sum_24h": _sum_recent_funding(
                funding_rows,
                current_time_ms=row.close_time_ms,
                window_ms=24 * 60 * 60 * 1000,
            ),
            "sum_3d": _sum_recent_funding(
                funding_rows,
                current_time_ms=row.close_time_ms,
                window_ms=3 * 24 * 60 * 60 * 1000,
            ),
        }

        return {
            "symbol": self._symbol,
            "market": market_payload,
            "timestamp": datetime.fromtimestamp(row.open_time_ms / 1000, tz=timezone.utc).isoformat(),
            "open_time": row.open_time_ms,
            "close_time": row.close_time_ms,
        }


class _ReplaySnapshotProvider:
    def __init__(self, frames: list[_ReplayFrame]) -> None:
        self._frames = frames
        self._index = -1

    def __call__(self) -> dict[str, Any]:
        self._index += 1
        if self._index >= len(self._frames):
            return {}
        frame = self._frames[self._index]
        payload: dict[str, Any] = {
            "symbol": frame.symbol,
            "market": frame.market,
            "meta": frame.meta,
            "tick": self._index,
        }
        return payload


class _HistoricalPortfolioSnapshotProvider:
    def __init__(
        self,
        *,
        candles_by_symbol: dict[str, dict[str, list[_Kline15m]]],
        premium_by_symbol: dict[str, list[_Kline15m]] | None = None,
        funding_by_symbol: dict[str, list[_FundingRateRow]] | None = None,
        market_intervals: list[str] | None = None,
        history_limit: int = 260,
    ) -> None:
        self._symbols = sorted(
            str(symbol).strip().upper() for symbol in candles_by_symbol.keys() if str(symbol).strip()
        )
        configured_intervals = list(market_intervals or ["15m", "1h", "4h"])
        if "15m" not in configured_intervals:
            configured_intervals.insert(0, "15m")
        seen_intervals: set[str] = set()
        self._intervals: list[str] = []
        for interval in configured_intervals:
            normalized = str(interval).strip()
            if not normalized or normalized in seen_intervals:
                continue
            seen_intervals.add(normalized)
            self._intervals.append(normalized)

        self._limit = max(int(history_limit), 1)
        self._histories: dict[str, dict[str, list[_Bar]]] = {}
        self._sources: dict[str, dict[str, list[_Kline15m]]] = {}
        self._source_index: dict[str, dict[str, int]] = {}
        self._premium_sources: dict[str, list[_Kline15m]] = {}
        self._premium_index: dict[str, int] = {}
        self._premium_histories: dict[str, deque[_Kline15m]] = {}
        self._funding_sources: dict[str, list[_FundingRateRow]] = {}
        self._funding_index: dict[str, int] = {}
        self._funding_histories: dict[str, deque[_FundingRateRow]] = {}
        self._latest_rows: dict[str, _Kline15m | None] = {}
        timeline: set[int] = set()

        for symbol in self._symbols:
            raw = candles_by_symbol.get(symbol, {})
            self._histories[symbol] = {interval: [] for interval in self._intervals}
            self._sources[symbol] = {}
            self._source_index[symbol] = {}
            self._premium_sources[symbol] = sorted(
                list((premium_by_symbol or {}).get(symbol, [])),
                key=lambda row: int(row.open_time_ms),
            )
            self._premium_index[symbol] = -1
            self._premium_histories[symbol] = deque(maxlen=max(self._limit, 320))
            self._funding_sources[symbol] = sorted(
                list((funding_by_symbol or {}).get(symbol, [])),
                key=lambda row: int(row.funding_time_ms),
            )
            self._funding_index[symbol] = -1
            self._funding_histories[symbol] = deque(maxlen=32)
            self._latest_rows[symbol] = None
            for interval in self._intervals:
                rows = sorted(list(raw.get(interval, [])), key=lambda row: int(row.open_time_ms))
                self._sources[symbol][interval] = rows
                self._source_index[symbol][interval] = -1
                if interval == "15m":
                    for row in rows:
                        timeline.add(int(row.open_time_ms))

        self._timeline = sorted(timeline)
        self._idx = -1

    def __len__(self) -> int:
        return len(self._timeline)

    @staticmethod
    def _row_to_ohlc(row: _Kline15m) -> _Bar:
        return _Bar(
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
        )

    def _append_history(self, *, symbol: str, interval: str, row: _Kline15m) -> None:
        history = self._histories[symbol][interval]
        history.append(self._row_to_ohlc(row))
        if len(history) > self._limit:
            del history[0]

    def _advance_symbol_15m(self, *, symbol: str, open_time_ms: int) -> None:
        rows = self._sources[symbol].get("15m", [])
        idx = int(self._source_index[symbol].get("15m", -1))
        while idx + 1 < len(rows):
            nxt = rows[idx + 1]
            nxt_open_time = int(nxt.open_time_ms)
            if nxt_open_time > int(open_time_ms):
                break
            idx += 1
            self._append_history(symbol=symbol, interval="15m", row=nxt)
            self._latest_rows[symbol] = nxt
            if nxt_open_time == int(open_time_ms):
                break
        self._source_index[symbol]["15m"] = idx

    def _advance_interval(
        self,
        *,
        symbol: str,
        interval: str,
        current_close_time_ms: int,
    ) -> None:
        rows = self._sources[symbol].get(interval, [])
        idx = int(self._source_index[symbol].get(interval, -1))
        while idx + 1 < len(rows):
            nxt = rows[idx + 1]
            if int(nxt.close_time_ms) > int(current_close_time_ms):
                break
            idx += 1
            self._append_history(symbol=symbol, interval=interval, row=nxt)
        self._source_index[symbol][interval] = idx

    def _advance_premium(self, *, symbol: str, current_close_time_ms: int) -> None:
        rows = self._premium_sources.get(symbol, [])
        idx = int(self._premium_index.get(symbol, -1))
        while idx + 1 < len(rows):
            nxt = rows[idx + 1]
            if int(nxt.close_time_ms) > int(current_close_time_ms):
                break
            idx += 1
            self._premium_histories[symbol].append(nxt)
        self._premium_index[symbol] = idx

    def _advance_funding(self, *, symbol: str, current_close_time_ms: int) -> None:
        rows = self._funding_sources.get(symbol, [])
        idx = int(self._funding_index.get(symbol, -1))
        while idx + 1 < len(rows):
            nxt = rows[idx + 1]
            if int(nxt.funding_time_ms) > int(current_close_time_ms):
                break
            idx += 1
            self._funding_histories[symbol].append(nxt)
        self._funding_index[symbol] = idx

    def current_candles(self) -> dict[str, dict[str, float]]:
        payload: dict[str, dict[str, float]] = {}
        for symbol, row in self._latest_rows.items():
            if row is None:
                continue
            payload[symbol] = {
                "open_time_ms": float(row.open_time_ms),
                "close_time_ms": float(row.close_time_ms),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.volume),
            }
        return payload

    def __call__(self) -> dict[str, Any]:
        self._idx += 1
        if self._idx >= len(self._timeline):
            return {}
        open_time_ms = int(self._timeline[self._idx])
        close_time_ms = open_time_ms + (15 * 60 * 1000)

        for symbol in self._symbols:
            self._advance_symbol_15m(symbol=symbol, open_time_ms=open_time_ms)
            latest = self._latest_rows.get(symbol)
            if latest is None:
                continue
            current_close_ms = int(latest.close_time_ms)
            for interval in self._intervals:
                if interval == "15m":
                    continue
                self._advance_interval(
                    symbol=symbol,
                    interval=interval,
                    current_close_time_ms=current_close_ms,
                )
            self._advance_premium(symbol=symbol, current_close_time_ms=current_close_ms)
            self._advance_funding(symbol=symbol, current_close_time_ms=current_close_ms)

        symbols_payload: dict[str, dict[str, Any]] = {}
        for symbol in self._symbols:
            latest = self._latest_rows.get(symbol)
            if latest is None:
                continue
            premium_rows = list(self._premium_histories.get(symbol, deque()))
            funding_rows = list(self._funding_histories.get(symbol, deque()))
            symbol_payload = {
                interval: self._histories[symbol][interval] for interval in self._intervals
            }
            symbol_payload["premium"] = {
                "close_15m": float(premium_rows[-1].close) if premium_rows else None,
                "zscore_24h": (
                    _zscore_latest([float(item.close) for item in premium_rows], 96)
                    if premium_rows
                    else None
                ),
                "zscore_3d": (
                    _zscore_latest([float(item.close) for item in premium_rows], 288)
                    if premium_rows
                    else None
                ),
            }
            symbol_payload["funding"] = {
                "last": float(funding_rows[-1].funding_rate) if funding_rows else None,
                "sum_24h": _sum_recent_funding(
                    funding_rows,
                    current_time_ms=latest.close_time_ms,
                    window_ms=24 * 60 * 60 * 1000,
                ),
                "sum_3d": _sum_recent_funding(
                    funding_rows,
                    current_time_ms=latest.close_time_ms,
                    window_ms=3 * 24 * 60 * 60 * 1000,
                ),
            }
            symbols_payload[symbol] = symbol_payload

        if not symbols_payload:
            return {}
        primary_symbol = next(iter(symbols_payload.keys()))
        return {
            "symbol": primary_symbol,
            "symbols": symbols_payload,
            "market": symbols_payload[primary_symbol],
            "timestamp": datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).isoformat(),
            "open_time": open_time_ms,
            "close_time": close_time_ms,
            "candles": self.current_candles(),
        }
