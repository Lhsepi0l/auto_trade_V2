from __future__ import annotations

from apps.trader_engine.services.decision_service import DecisionService
from apps.trader_engine.services.market_data_service import Candle


def _mk_candles(prices: list[float]) -> list[Candle]:
    out: list[Candle] = []
    t = 0
    for p in prices:
        out.append(
            Candle(
                open_time_ms=t,
                open=p,
                high=p * 1.001,
                low=p * 0.999,
                close=p,
                volume=1.0,
                close_time_ms=t + 1,
            )
        )
        t += 60_000
    return out


def test_score_symbol_trend_positive_for_uptrend():
    svc = DecisionService(vol_shock_threshold_pct=100.0)  # disable vol shock tagging
    up = [100.0 + i for i in range(120)]
    candles = _mk_candles(up)
    s = svc.score_symbol(symbol="BTCUSDT", candles_by_interval={"30m": candles, "1h": candles, "4h": candles})
    assert s.long_score >= s.short_score
    assert s.composite > 0


def test_pick_candidate_respects_threshold_and_vol_shock():
    svc = DecisionService(vol_shock_threshold_pct=0.01)  # will mark as VOL_SHOCK
    prices = [100.0 + i for i in range(120)]
    candles = _mk_candles(prices)
    s = svc.score_symbol(symbol="BTCUSDT", candles_by_interval={"30m": candles, "1h": candles, "4h": candles})
    assert s.vol_tag == "VOL_SHOCK"
    cand = svc.pick_candidate(scores=[s], score_threshold=0.1)
    assert cand is None

