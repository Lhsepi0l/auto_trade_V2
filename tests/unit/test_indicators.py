from __future__ import annotations

from dataclasses import replace

import pytest

from apps.trader_engine.services.indicators import atr_mult, ema, roc, rsi
from tests.fixtures.fake_exchange import fake_candle_series


@pytest.mark.unit
def test_indicator_sanity_values() -> None:
    xs = [1, 2, 3, 4, 5, 6, 7, 8]
    assert ema(xs, 3) is not None
    assert rsi(xs, 3) is not None
    assert roc(xs, 3) is not None


@pytest.mark.unit
def test_atr_mult_vol_shock_threshold() -> None:
    candles = fake_candle_series("BTCUSDT", "30m", count=120, base=100.0)
    # Inject a final volatility spike
    for i in range(len(candles) - 5, len(candles)):
        c = candles[i]
        candles[i] = replace(c, high=c.close * 1.05, low=c.close * 0.95)
    am = atr_mult(candles, period=14, mean_window=50)
    assert am is not None
    assert am.mult >= 2.5
