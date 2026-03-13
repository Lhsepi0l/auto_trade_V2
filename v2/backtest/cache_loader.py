from __future__ import annotations

import csv
from pathlib import Path

from v2.backtest.cache_paths import _interval_to_ms
from v2.backtest.snapshots import _FundingRateRow, _Kline15m


def _write_klines_csv(*, path: Path, symbol: str, rows: list[_Kline15m]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(
            ["symbol", "open_time_ms", "open", "high", "low", "close", "volume", "close_time_ms"]
        )
        for row in rows:
            writer.writerow(
                [
                    symbol,
                    row.open_time_ms,
                    f"{row.open:.8f}",
                    f"{row.high:.8f}",
                    f"{row.low:.8f}",
                    f"{row.close:.8f}",
                    f"{row.volume:.8f}",
                    row.close_time_ms,
                ]
            )


def _klines_csv_has_volume_column(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        with path.open("r", encoding="utf-8", newline="") as fp:
            header = fp.readline().strip().split(",")
    except OSError:
        return False
    return "volume" in {str(item).strip() for item in header}


def _read_klines_csv_rows(path: Path) -> list[_Kline15m]:
    if not path.exists() or not path.is_file():
        return []
    rows: list[_Kline15m] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as fp:
            reader = csv.DictReader(fp)
            for item in reader:
                if not isinstance(item, dict):
                    continue
                try:
                    rows.append(
                        _Kline15m(
                            open_time_ms=int(item.get("open_time_ms") or 0),
                            close_time_ms=int(item.get("close_time_ms") or 0),
                            open=float(item.get("open") or 0.0),
                            high=float(item.get("high") or 0.0),
                            low=float(item.get("low") or 0.0),
                            close=float(item.get("close") or 0.0),
                            volume=float(item.get("volume") or 0.0),
                        )
                    )
                except (TypeError, ValueError):
                    continue
    except OSError:
        return []
    rows.sort(key=lambda row: row.open_time_ms)
    return rows


def _load_cached_klines_for_range(
    *,
    path: Path,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[_Kline15m]:
    rows = _read_klines_csv_rows(path)
    if not rows:
        return []

    interval_ms = _interval_to_ms(interval)
    filtered = [
        row
        for row in rows
        if int(row.open_time_ms) >= int(start_ms) and int(row.open_time_ms) < int(end_ms)
    ]
    if not filtered:
        return []

    expected = max(int((int(end_ms) - int(start_ms)) // max(interval_ms, 1)), 1)
    coverage_ok = int(filtered[0].open_time_ms) <= int(start_ms) + interval_ms and int(
        filtered[-1].open_time_ms
    ) >= int(end_ms) - (interval_ms * 2)
    density_ok = len(filtered) >= int(expected * 0.90)
    if coverage_ok and density_ok:
        return filtered
    return []


def _load_cached_premium_for_range(
    *,
    path: Path,
    start_ms: int,
    end_ms: int,
) -> list[_Kline15m]:
    return _load_cached_klines_for_range(
        path=path,
        interval="15m",
        start_ms=start_ms,
        end_ms=end_ms,
    )


def _write_funding_csv(*, path: Path, symbol: str, rows: list[_FundingRateRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["symbol", "funding_time_ms", "funding_rate"])
        for row in rows:
            writer.writerow(
                [
                    symbol,
                    int(row.funding_time_ms),
                    f"{row.funding_rate:.10f}",
                ]
            )


def _read_funding_csv_rows(path: Path) -> list[_FundingRateRow]:
    if not path.exists() or not path.is_file():
        return []
    rows: list[_FundingRateRow] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as fp:
            reader = csv.DictReader(fp)
            for item in reader:
                if not isinstance(item, dict):
                    continue
                try:
                    rows.append(
                        _FundingRateRow(
                            funding_time_ms=int(item.get("funding_time_ms") or 0),
                            funding_rate=float(item.get("funding_rate") or 0.0),
                        )
                    )
                except (TypeError, ValueError):
                    continue
    except OSError:
        return []
    rows.sort(key=lambda row: row.funding_time_ms)
    return rows


def _load_cached_funding_for_range(
    *,
    path: Path,
    start_ms: int,
    end_ms: int,
) -> list[_FundingRateRow]:
    rows = _read_funding_csv_rows(path)
    if not rows:
        return []
    filtered = [
        row
        for row in rows
        if int(row.funding_time_ms) >= int(start_ms) and int(row.funding_time_ms) < int(end_ms)
    ]
    if not filtered:
        return []
    coverage_ok = int(filtered[0].funding_time_ms) <= int(start_ms) + (8 * 60 * 60 * 1000) and int(
        filtered[-1].funding_time_ms
    ) >= int(end_ms) - (2 * 8 * 60 * 60 * 1000)
    if coverage_ok:
        return filtered
    return []
