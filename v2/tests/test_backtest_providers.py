from __future__ import annotations

from v2.backtest.providers import _HistoricalPortfolioSnapshotProvider
from v2.backtest.snapshots import _Kline15m


def _rows(interval_ms: int, closes: list[float]) -> list[_Kline15m]:
    rows: list[_Kline15m] = []
    for idx, close in enumerate(closes):
        open_time_ms = idx * interval_ms
        rows.append(
            _Kline15m(
                open_time_ms=open_time_ms,
                close_time_ms=open_time_ms + interval_ms,
                open=float(close),
                high=float(close) + 1.0,
                low=float(close) - 1.0,
                close=float(close),
                volume=100.0,
            )
        )
    return rows


def test_historical_portfolio_snapshot_provider_uses_configured_base_interval() -> None:
    provider = _HistoricalPortfolioSnapshotProvider(
        candles_by_symbol={
            "BTCUSDT": {
                "5m": _rows(5 * 60 * 1000, [100.0, 101.0, 102.0]),
                "30m": _rows(30 * 60 * 1000, [100.0]),
                "2h": _rows(2 * 60 * 60 * 1000, [100.0]),
                "12h": _rows(12 * 60 * 60 * 1000, [100.0]),
            }
        },
        market_intervals=["5m", "30m", "2h", "12h"],
        history_limit=32,
    )

    assert len(provider) == 3

    first = provider()
    assert first["candles"]["BTCUSDT"]["close"] == 100.0
    assert len(first["symbols"]["BTCUSDT"]["5m"]) == 1

