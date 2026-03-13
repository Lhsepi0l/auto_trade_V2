from __future__ import annotations

import asyncio
from typing import Callable

from v2.backtest.cache_paths import _interval_to_ms
from v2.backtest.snapshots import _FundingRateRow, _Kline15m
from v2.exchange import BinanceRESTClient


def _aggregate_klines_to_interval(
    rows: list[_Kline15m], *, target_interval_ms: int
) -> list[_Kline15m]:
    sorted_rows = sorted(rows, key=lambda row: row.open_time_ms)
    grouped: list[_Kline15m] = []
    active_bucket: int | None = None
    active: _Kline15m | None = None

    for row in sorted_rows:
        bucket = int(row.open_time_ms // target_interval_ms)
        if active_bucket is None or active is None:
            active_bucket = bucket
            active = row
            continue
        if bucket != active_bucket:
            grouped.append(active)
            active_bucket = bucket
            active = row
            continue
        active = _Kline15m(
            open_time_ms=active.open_time_ms,
            close_time_ms=max(int(active.close_time_ms), int(row.close_time_ms)),
            open=float(active.open),
            high=max(float(active.high), float(row.high)),
            low=min(float(active.low), float(row.low)),
            close=float(row.close),
            volume=float(active.volume) + float(row.volume),
        )

    if active is not None:
        grouped.append(active)
    return grouped


async def _fetch_klines_interval(
    *,
    rest_client: BinanceRESTClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    on_progress: Callable[[int], None] | None = None,
    sleep_sec: float = 0.03,
) -> list[_Kline15m]:
    interval_code = str(interval).strip().lower()
    request_interval = "5m" if interval_code == "10m" else interval_code
    request_interval_ms = _interval_to_ms(request_interval)
    current = int(start_ms)
    rows: list[_Kline15m] = []
    seen_open_time: set[int] = set()
    span_ms = max(int(end_ms) - int(start_ms), 1)
    next_mark = 0

    if on_progress is not None:
        on_progress(0)
        next_mark = 5

    while current < end_ms:
        payload = await rest_client.public_request(
            "GET",
            "/fapi/v1/klines",
            params={
                "symbol": symbol,
                "interval": request_interval,
                "startTime": current,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        if not isinstance(payload, list) or len(payload) == 0:
            break

        fetched = 0
        for item in payload:
            if not isinstance(item, (list, tuple)) or len(item) < 7:
                continue
            try:
                open_time_ms = int(item[0])
                close_time_ms = int(item[6])
                o = float(item[1])
                h = float(item[2])
                low_price = float(item[3])
                c = float(item[4])
                volume = float(item[5])
            except (TypeError, ValueError):
                continue
            if open_time_ms < start_ms or open_time_ms >= end_ms:
                continue
            if open_time_ms in seen_open_time:
                continue
            seen_open_time.add(open_time_ms)
            rows.append(
                _Kline15m(
                    open_time_ms=open_time_ms,
                    close_time_ms=close_time_ms,
                    open=o,
                    high=h,
                    low=low_price,
                    close=c,
                    volume=volume,
                )
            )
            fetched += 1

        if fetched == 0:
            break
        last_open_ms = rows[-1].open_time_ms
        current = last_open_ms + request_interval_ms
        if on_progress is not None:
            progress_ms = min(int(current), int(end_ms))
            progress_pct = max(0, min(100, int(((progress_ms - int(start_ms)) * 100) / span_ms)))
            while next_mark <= progress_pct:
                on_progress(next_mark)
                next_mark += 5
        delay = max(float(sleep_sec), 0.0)
        if delay > 0.0:
            await asyncio.sleep(delay)

    if on_progress is not None:
        while next_mark <= 100:
            on_progress(next_mark)
            next_mark += 5

    rows.sort(key=lambda row: row.open_time_ms)
    if interval_code == "10m":
        return _aggregate_klines_to_interval(rows, target_interval_ms=_interval_to_ms("10m"))
    return rows


async def _fetch_klines_15m(
    *,
    rest_client: BinanceRESTClient,
    symbol: str,
    start_ms: int,
    end_ms: int,
    on_progress: Callable[[int], None] | None = None,
    sleep_sec: float = 0.03,
) -> list[_Kline15m]:
    return await _fetch_klines_interval(
        rest_client=rest_client,
        symbol=symbol,
        interval="15m",
        start_ms=start_ms,
        end_ms=end_ms,
        on_progress=on_progress,
        sleep_sec=sleep_sec,
    )


async def _fetch_premium_index_klines_15m(
    *,
    rest_client: BinanceRESTClient,
    symbol: str,
    start_ms: int,
    end_ms: int,
    on_progress: Callable[[int], None] | None = None,
    sleep_sec: float = 0.03,
) -> list[_Kline15m]:
    request_interval_ms = _interval_to_ms("15m")
    current = int(start_ms)
    rows: list[_Kline15m] = []
    seen_open_time: set[int] = set()
    span_ms = max(int(end_ms) - int(start_ms), 1)
    next_mark = 0

    if on_progress is not None:
        on_progress(0)
        next_mark = 5

    while current < end_ms:
        payload = await rest_client.public_request(
            "GET",
            "/fapi/v1/premiumIndexKlines",
            params={
                "symbol": symbol,
                "interval": "15m",
                "startTime": current,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        if not isinstance(payload, list) or len(payload) == 0:
            break

        fetched = 0
        for item in payload:
            if not isinstance(item, (list, tuple)) or len(item) < 7:
                continue
            try:
                open_time_ms = int(item[0])
                close_time_ms = int(item[6])
                o = float(item[1])
                h = float(item[2])
                low_price = float(item[3])
                c = float(item[4])
            except (TypeError, ValueError):
                continue
            if open_time_ms < start_ms or open_time_ms >= end_ms:
                continue
            if open_time_ms in seen_open_time:
                continue
            seen_open_time.add(open_time_ms)
            rows.append(
                _Kline15m(
                    open_time_ms=open_time_ms,
                    close_time_ms=close_time_ms,
                    open=o,
                    high=h,
                    low=low_price,
                    close=c,
                    volume=0.0,
                )
            )
            fetched += 1

        if fetched == 0:
            break
        last_open_ms = rows[-1].open_time_ms
        current = last_open_ms + request_interval_ms
        if on_progress is not None:
            progress_ms = min(int(current), int(end_ms))
            progress_pct = max(0, min(100, int(((progress_ms - int(start_ms)) * 100) / span_ms)))
            while next_mark <= progress_pct:
                on_progress(next_mark)
                next_mark += 5
        delay = max(float(sleep_sec), 0.0)
        if delay > 0.0:
            await asyncio.sleep(delay)

    if on_progress is not None:
        while next_mark <= 100:
            on_progress(next_mark)
            next_mark += 5

    rows.sort(key=lambda row: row.open_time_ms)
    return rows


async def _fetch_funding_rate_history(
    *,
    rest_client: BinanceRESTClient,
    symbol: str,
    start_ms: int,
    end_ms: int,
    sleep_sec: float = 0.03,
) -> list[_FundingRateRow]:
    current = int(start_ms)
    rows: list[_FundingRateRow] = []
    seen_funding_time: set[int] = set()

    while current < end_ms:
        payload = await rest_client.public_request(
            "GET",
            "/fapi/v1/fundingRate",
            params={
                "symbol": symbol,
                "startTime": current,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        if not isinstance(payload, list) or len(payload) == 0:
            break

        fetched = 0
        last_funding_time_ms: int | None = None
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                funding_time_ms = int(item.get("fundingTime") or 0)
                funding_rate = float(item.get("fundingRate") or 0.0)
            except (TypeError, ValueError):
                continue
            if funding_time_ms < start_ms or funding_time_ms >= end_ms:
                continue
            last_funding_time_ms = funding_time_ms
            if funding_time_ms in seen_funding_time:
                continue
            seen_funding_time.add(funding_time_ms)
            rows.append(
                _FundingRateRow(
                    funding_time_ms=funding_time_ms,
                    funding_rate=funding_rate,
                )
            )
            fetched += 1

        if fetched == 0:
            break
        if last_funding_time_ms is None:
            break
        current = int(last_funding_time_ms) + 1
        delay = max(float(sleep_sec), 0.0)
        if delay > 0.0:
            await asyncio.sleep(delay)

    rows.sort(key=lambda row: row.funding_time_ms)
    return rows
