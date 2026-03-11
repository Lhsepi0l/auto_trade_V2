from __future__ import annotations

from v2.config.loader import load_effective_config
from v2.run import _HistoricalSnapshotProvider, _Kline15m, _resolve_market_intervals


def _kline(open_time: int, close_time: int, close: float) -> _Kline15m:
    return _Kline15m(
        open_time_ms=open_time,
        close_time_ms=close_time,
        open=close - 0.2,
        high=close + 0.4,
        low=close - 0.4,
        close=close,
    )


def test_resolve_market_intervals_preserves_order_and_dedupes() -> None:
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow")
    cfg.behavior.exchange.market_intervals = ["4h", "15m", "1h", "4h"]

    intervals = _resolve_market_intervals(cfg)

    assert intervals == ["4h", "15m", "1h"]


def test_historical_provider_uses_configured_intervals_only() -> None:
    candles_15m = [
        _kline(900_000, 1_799_999, 100.0),
        _kline(1_800_000, 2_699_999, 101.0),
    ]
    market_candles = {
        "10m": [_kline(600_000, 1_199_999, 99.5)],
        "30m": [_kline(0, 1_799_999, 100.0)],
        "1h": [_kline(0, 3_599_999, 101.0)],
        "4h": [_kline(0, 14_399_999, 102.0)],
    }

    provider = _HistoricalSnapshotProvider(
        symbol="BTCUSDT",
        candles_15m=candles_15m,
        market_candles=market_candles,
        market_intervals=["15m", "1h", "4h"],
    )
    snapshot = provider()
    market = snapshot.get("market", {})

    assert isinstance(market, dict)
    assert list(market.keys()) == ["15m", "1h", "4h", "premium", "funding"]
    assert "10m" not in market
    assert "30m" not in market
    assert "premium" in market
    assert "funding" in market
