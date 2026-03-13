from __future__ import annotations

from pathlib import Path


def _interval_to_ms(interval: str) -> int:
    value = str(interval).strip().lower()
    if len(value) >= 2:
        qty = int(value[:-1]) if value[:-1].isdigit() else 0
        unit = value[-1]
        if qty > 0 and unit == "m":
            return int(qty * 60 * 1000)
        if qty > 0 and unit == "h":
            return int(qty * 60 * 60 * 1000)
        if qty > 0 and unit == "d":
            return int(qty * 24 * 60 * 60 * 1000)
    return 15 * 60 * 1000


def _cache_file_for_klines(*, cache_root: Path, symbol: str, interval: str, years: int) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    code = str(interval).strip().lower()
    return cache_root / f"klines_{str(symbol).strip().lower()}_{code}_{int(years)}y.csv"


def _cache_file_for_premium(*, cache_root: Path, symbol: str, interval: str, years: int) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    code = str(interval).strip().lower()
    return cache_root / f"premium_{str(symbol).strip().lower()}_{code}_{int(years)}y.csv"


def _cache_file_for_funding(*, cache_root: Path, symbol: str, years: int) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    return cache_root / f"funding_{str(symbol).strip().lower()}_{int(years)}y.csv"
